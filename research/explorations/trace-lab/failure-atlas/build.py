"""Conservative source-grounded failure atlas consumer (HAR-131).

Reads the transient trace-query views (owned by the query worker) and emits
a small reproducible atlas: ``atlas.json`` + ``README.md``.

Every number is computed from current view rows. ``counts_verdict`` is the
sole counted authority; unknown stays unknown. Import/connect errors fail
visibly -- there is no fallback ingestor and no hardcoded stats.

Usage (after the parent integrates ``evallab.trace_query``)::

    PYTHONPATH=src python research/explorations/trace-lab/failure-atlas/build.py \
        --repo-root . --out-dir research/explorations/trace-lab/failure-atlas

Optional explicit corpus pinning and GEPA gate::

    --job-dir PATH (repeatable) --results-home PATH --derived-root PATH
    --g2-bindings PATH  # authorized G2 training task+package bindings JSON
"""

from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

MIMO_MODEL_RE = re.compile(r"MiMo-V2\.6-Distill-Qwen-9B$")
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
    """Authoritative Python-task ledger: task_id -> status/run/run_digest."""
    ledger_path = repo_root / "research/experiments/python-task-ledger/ledger.csv"
    by_task: dict = {}
    with ledger_path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            by_task[row["task_id"]] = {
                "status": row.get("status"),
                "run": row.get("run"),
                "run_digest": row.get("run_digest"),
            }
    return {"path": "research/experiments/python-task-ledger/ledger.csv",
            "sha256": _sha256_file(ledger_path), "n_tasks": len(by_task), "by_task": by_task}


def is_python_eligible(trial: dict) -> tuple[bool, str]:
    """MiMo-V2.6-Distill-Qwen-9B model AND format-code-task family (recorded identity)."""
    model = trial.get("model_name") or ""
    task_name = trial.get("task_name") or ""
    family = task_name.split("/")[-1]
    if not MIMO_MODEL_RE.search(model):
        return False, f"non-eligible model: {model or 'null'}"
    if not PYTHON_TASK_RE.fullmatch(family):
        return False, f"non-Python task family: {task_name}"
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
                       prefer_last: bool = False) -> tuple[dict, str]:
    """Deterministic step pick; refs must resolve to a real view step (raw check is separate)."""
    ordered = _ordered_real_steps(steps)
    fresh = [s for s in ordered if not s.get("is_copied_context")] or ordered
    fresh_by_ref = {s.get("step_ref"): s for s in fresh}
    candidates: list[tuple[dict | None, str]] = []
    loop = judgments.get("loop_kind") or {}
    if isinstance(loop, dict) and loop.get("loop_onset_step") is not None:
        ref = f"head#{loop['loop_onset_step']}"
        candidates.append((fresh_by_ref.get(ref), f"loop_onset {ref}"))
    ff = judgments.get("first_failure")
    ff_ref = (ff or {}).get("step") if isinstance(ff, dict) else None
    if ff_ref is None and trial.get("first_failure_ref"):
        ff_ref = trial["first_failure_ref"]
    if _step_number(ff_ref) is not None:
        ref = f"head#{_step_number(ff_ref)}"
        candidates.append((fresh_by_ref.get(ref), f"first_failure {ref}"))
    for entry in taint:
        if isinstance(entry, dict) and entry.get("evidence"):
            ref = str(entry["evidence"])
            candidates.append((fresh_by_ref.get(ref), f"taint evidence {ref}"))
            break
    for step, how in candidates:
        if step is not None and (step.get("command_text") or step.get("observation_excerpt")):
            return step, how
    if prefer_last and fresh:
        return fresh[-1], "last real non-copied step (budget tail)"
    if fresh:
        return fresh[len(fresh) // 2], "median real non-copied step (no recorded ref resolved)"
    return ordered[len(ordered) // 2], "median real step (only copied context available)"


FROZEN_LABEL_COHORTS = {"har109", "har119"}


def summarize_labels(trial: dict) -> tuple[list, int]:
    """Frozen rater entries only; heuristic parquet rows are dropped and counted."""
    out, dropped = [], 0
    for entry in _parse_json_list(trial.get("labels_json")):
        if not isinstance(entry, dict):
            continue
        if entry.get("cohort") not in FROZEN_LABEL_COHORTS:
            dropped += 1
            continue
        out.append({"cohort": entry.get("cohort"), "rater": entry.get("rater"),
                    "provenance": entry.get("provenance")})
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


def build_exemplar(trial: dict, steps: list, prefer_last: bool = False) -> dict:
    judgments = _parse_json_dict(trial.get("decision_judgments_json"))
    taint = _parse_json_list(trial.get("taint_json"))
    step, how = pick_exemplar_step(trial, steps, judgments, taint, prefer_last=prefer_last)
    labels, _ = summarize_labels(trial)
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
                     prefer_last: bool = False) -> list:
    """First `limit` RAW-VERIFIED exemplars (by trial_name); unverified refs are skipped, never padded."""
    exemplars = []
    for trial in sorted(matching, key=lambda t: t.get("trial_name") or ""):
        if len(exemplars) == limit:
            break
        steps = steps_by_trial.get((trial.get("job_id"), trial.get("trial_id")), [])
        if not _ordered_real_steps(steps):
            continue
        candidate = build_exemplar(trial, steps, prefer_last=prefer_last)
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
        "denominator": "eligible trials with loop_kind non-null (decision v2 present)",
        "lever_hypothesis": "Prompt/harness completion discipline (e.g. confirm-then-stop); causal evidence absent.",
    },
    {
        "id": "repetition-loop",
        "kind": "opinion",
        "predicate": "decision loop_kind.kind == 'repetition' (producer rule HAR-119)",
        "denominator": "eligible trials with loop_kind non-null (decision v2 present)",
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
        "predicate": "processed taint contains an upstream_fetch entry (recorded command, NOT proof of copying)",
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


def build_atlas(trials: list, steps_by_trial: dict, coverage: dict, repo_root: Path,
                ledger: dict, freeze: dict, page_scores: dict, g2_bindings: object) -> dict:
    eligible, excluded_rows = [], []
    for trial in trials:
        ok, reason = is_python_eligible(trial)
        (eligible if ok else excluded_rows).append(trial if ok else {**trial, "exclusion_reason": reason})
    for trial in eligible:
        entry = ledger["by_task"].get(task_id_of(trial))
        trial["_ledger_status"] = entry["status"] if entry else "absent-from-ledger"
        trial["_ledger_run"] = entry["run"] if entry else None

    opinion_limits = {
        "loop_producer": "trial_decision.classify_loop_kind (rule HAR-119)",
        "loop_calibration": "7/11 vs rater-agreed loop_kind on HAR-119 part 2 (12 HAR-110 split-v2 runs); "
                            "measured on HAR-119 only, NOT transferred to other cohorts",
        "loop_calibration_provenance": "research/explorations/trace-lab/har119/page_scores.json",
        "first_failure_limits": "page first_failure 1/9 vs agreed with 1/12 coverage and 11 abstentions; "
                               "page values are usually absent -- do not use as step truth",
        "blame_limits": "page blame 11/11 vs agreed is a near-constant prior, not skill",
        "rater_agreement": "loop_kind 11/12; first_failure 9/12; blame 11/12 (near-constant model prior)",
    }

    heuristic_labels_dropped = 0
    for trial in eligible:
        _, dropped = summarize_labels(trial)
        heuristic_labels_dropped += dropped

    categories = []
    for cat in CATEGORY_DEFS:
        denom = denominator_for(cat["id"], eligible)
        matching = [t for t in denom if match_category(cat["id"], t)]
        exemplars = select_exemplars(matching, steps_by_trial,
                                     prefer_last=(cat["id"] == "recorded-budget-stop"))
        categories.append({
            **cat,
            "n": len(matching),
            "denominator_n": len(denom),
            "excluded_from_denominator": len(eligible) - len(denom),
            "trial_names": sorted(t.get("trial_name") for t in matching),
            "exemplars": exemplars,
            "generalized": len(exemplars) >= 2,
            "opinion_limits": opinion_limits if cat["kind"] in ("opinion",) else None,
        })

    rare_cases = []
    discarded = [t for t in eligible if t.get("_ledger_status") == "discarded"]
    if discarded:
        rare_cases.append({
            "id": "ledger-discarded-task",
            "note": "Trial(s) on ledger-discarded (authoritatively task-broken) tasks; n<2 stays a rare case, never a generalized category.",
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
        })

    reflection = reflection_status(g2_bindings, eligible, steps_by_trial)

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
            "raw_reward": trial.get("raw_reward"), "scored": trial.get("scored"),
            "counts_verdict": trial.get("counts_verdict"),
            "counts_reasons_json": trial.get("counts_reasons_json"),
            "stop_reason": trial.get("stop_reason"), "labels_json": trial.get("labels_json"),
        }, sort_keys=True).encode())

    return {
        "atlas": "har131-failure-atlas",
        "status": "provisional",
        "status_reasons": [
            "Query dependency e27ccb7d is under independent-review fix; view arm/split/continuation/label fields are provisional.",
            "Published counts/pages are stale for HAR-81/85/90/104/110; parent reprocesses post-PR609 and rebuilds counts.",
            "No finished atlas claim until the query fix and the parent report refresh land; parent verifies the final corpus.",
        ],
        "generated_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "repo_head": repo_head,
        "atlas_input_digest": f"sha256:{input_hasher.hexdigest()}",
        "source_hashes": {
            "trace_query_py": trace_query_sha,
            "trace_query_note": "Runtime dependency owned by the query worker; exercised here as a local copy of commit e27ccb7d pending parent integration.",
            "ledger_csv": ledger["sha256"],
            "page_scores_json": _optional_sha(repo_root / "research/explorations/trace-lab/har119/page_scores.json"),
            "har119_manifest": freeze.get("manifest_sha256"),
            "har109_manifest": _optional_sha(repo_root / "research/explorations/trace-lab/har109/hand_labels.sha256"),
        },
        "provenance": {
            "har119_freeze": freeze,
            "page_scores_match_manifest": (
                page_scores.get("labels_manifest_sha256") in (freeze.get("manifest_sha256") or "")
            ),
            "heuristic_labels_dropped": heuristic_labels_dropped,
            "label_cohorts_kept": sorted(FROZEN_LABEL_COHORTS),
            "coverage": coverage,
        },
        "corpus": {
            "eligible_predicate": "model_name matches MiMo-V2.6-Distill-Qwen-9B AND task family format-code-task-NNNNNN (recorded identity, ledger corroborates family=Python)",
            "n_discovered": len(trials),
            "n_eligible": len(eligible),
            "excluded_rows": [
                {"trial_name": t.get("trial_name"), "card": t.get("card"),
                 "task_name": t.get("task_name"), "model_name": t.get("model_name"),
                 "reason": t.get("exclusion_reason")}
                for t in sorted(excluded_rows, key=lambda t: t.get("trial_name") or "")
            ],
        },
        "categories": categories,
        "rare_cases": rare_cases,
        "task_health": task_health,
        "opinion_limits": opinion_limits,
        "reflection": reflection,
        "refresh": {
            "command": "PYTHONPATH=src python research/explorations/trace-lab/failure-atlas/build.py --repo-root . --out-dir research/explorations/trace-lab/failure-atlas",
            "notes": "Re-run after the parent reprocesses stale counts/pages (post-PR609) and integrates evallab.trace_query; frequencies, exemplars and hashes update deterministically. New publishes appear on the next connection.",
        },
    }


def reflection_status(g2_bindings: object, eligible: list, steps_by_trial: dict) -> dict:
    """Dynamic GEPA reflection firewall: fail closed without authorized G2 bindings."""
    base = {
        "status": "unavailable",
        "payload": None,
        "payload_file": None,
        "note": "Full atlas is inspection-only. Safe reflection covers ONLY explicitly authorized "
                "G2 training task+package bindings; it must never carry held-out examples or "
                "held-out-derived aggregates to the proposer.",
    }
    if not g2_bindings:
        return {**base, "reason": "no authorized G2 training bindings supplied (--g2-bindings absent); "
                "G2 has not landed. Unavailable reflection is a dependency status, not an empty training atlas."}
    if not isinstance(g2_bindings, list) or not g2_bindings:
        return {**base, "reason": "G2 bindings file present but empty or malformed; fail closed."}
    allowed = set()
    for binding in g2_bindings:
        if isinstance(binding, dict) and binding.get("task") and binding.get("package_digest"):
            allowed.add((str(binding["task"]), str(binding["package_digest"])))
    if not allowed:
        return {**base, "reason": "no well-formed {task, package_digest} bindings; fail closed."}
    matched = [t for t in eligible
               if (task_id_of(t), t.get("task_package_digest")) in allowed]
    if not matched:
        return {**base, "reason": "bindings supplied but match zero eligible trials; fail closed with no payload."}
    return {
        "status": "available",
        "reason": f"{len(matched)} eligible trial(s) match authorized G2 training bindings.",
        "payload": None,
        "payload_file": None,
        "matched_trial_names": sorted(t.get("trial_name") for t in matched),
        "note": base["note"] + " Payload construction from matched trials is parent-owned at integration time.",
    }


def render_readme(atlas: dict) -> str:
    lines = [
        "# HAR-131 failure atlas (inspection-only, PROVISIONAL)",
        "",
        f"Generated {atlas['generated_at_utc']} from current trace-query rows. "
        "PROVISIONAL: the query surface is under independent-review fix and "
        "published counts/pages are stale pending the parent reprocess -- no finished "
        "atlas claim until the fix and parent refresh land. Every number below is "
        "computed from current rows; unknown stays unknown and `counts_verdict` is the "
        "sole counted authority. Overlapping patterns are not an exhaustive causal partition.",
        "",
        "## Corpus",
        "",
        f"- Discovered trials: {atlas['corpus']['n_discovered']}; "
        f"Python-eligible: {atlas['corpus']['n_eligible']} "
        f"({atlas['corpus']['eligible_predicate']})",
        f"- Excluded (reported, not dropped): {len(atlas['corpus']['excluded_rows'])} "
        "(non-Python family or non-MiMo model; see atlas.json)",
        f"- Coverage: missing_processed={atlas['provenance']['coverage'].get('missing_processed')}, "
        f"missing_counts={atlas['provenance']['coverage'].get('missing_counts')}, "
        f"missing_atif={atlas['provenance']['coverage'].get('missing_atif')}",
        "- Published counts/pages are stale for HAR-81/85/90/104/110 (counts only on HAR-116); "
        "parent reprocesses post-PR609 -- re-run the refresh command in atlas.json.",
        f"- Frozen labels independently re-verified ({atlas['provenance']['har119_freeze'].get('checked')} files "
        f"ok={atlas['provenance']['har119_freeze'].get('files_ok')}); heuristic label rows dropped: "
        f"{atlas['provenance'].get('heuristic_labels_dropped')}.",
        "",
        "## Patterns (frequency | eligible denominator | N, current view only)",
        "",
    ]
    for cat in atlas["categories"]:
        flag = "generalized" if cat["generalized"] else "RARE (fewer than 2 raw-verified step-linked exemplars)"
        lines.append(f"### {cat['id']} [{cat['kind']}; {flag}]")
        lines.append(f"- {cat['n']}/{cat['denominator_n']} ({cat['denominator']})")
        lines.append(f"- Predicate: `{cat['predicate']}`")
        if cat.get("opinion_limits"):
            lines.append("- Opinion limits: loop rule 7/11 vs rater-agreed (HAR-119 only); "
                         "first_failure usually absent; blame near-constant prior.")
        for ex in cat["exemplars"]:
            verified = ex.get("raw_verification", {}).get("verified")
            lines.append(
                f"- `{ex['trial_name']}` {ex['step_ref']} ({ex['source_path']}, "
                f"{ex.get('evidence_kind')}, raw-verified={verified}) :: {(ex['command_text'] or ex['observation_excerpt'] or '')[:120]}")
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
    return parser.parse_args(argv)


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
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

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

    atlas = build_atlas(trials, steps_by_trial, coverage, repo_root, ledger, freeze, page_scores, g2_bindings)
    (out_dir / "atlas.json").write_text(json.dumps(atlas, indent=1) + "\n", encoding="utf-8")
    (out_dir / "README.md").write_text(render_readme(atlas), encoding="utf-8")

    print(f"trials={len(trials)} eligible={atlas['corpus']['n_eligible']} "
          f"excluded={len(atlas['corpus']['excluded_rows'])}")
    for cat in atlas["categories"]:
        print(f"{cat['id']}: {cat['n']}/{cat['denominator_n']} exemplars={len(cat['exemplars'])} "
              f"generalized={cat['generalized']}")
    print(f"reflection={atlas['reflection']['status']}")
    print(f"wrote {out_dir / 'atlas.json'} and {out_dir / 'README.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
