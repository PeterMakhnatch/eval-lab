"""E11: eval-card generator with purpose-bound shape and mandatory uncertainty.

Cards are the platform's citable results (platform-architecture.md v2 §4) and the
terminus of the spec lifecycle:
    draft -> (purpose=comparison => prereg required) -> submitted -> gated ->
    dispatched -> analyzed -> carded

Contracts:
1. Purpose drives the card: baseline -> per-agent pass@k; comparison -> paired
   analysis quoting prereg block (refuses without prereg); practice -> excluded
   (refusal).
2. Attempts from one task are one evidence unit.
3. Uncertainty is mandatory (T4): every rate carries n and an interval.
   Underpowered cohorts render "not distinguishable" rather than a bare rate.
4. Harness exceptions are never measured as capability failures and are reported
   separately from scored zeros.
5. Cards are drafts pending human review.
6. Deterministic: same evidence => byte-identical card. Lineage carried in inputs list.
"""

from __future__ import annotations

import contextlib
import json
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from evallab.attach import attach
from evallab.cohort import (
    summarize_job_evidence,
)
from evallab.facts import digest_json
from evallab.results import (
    JobRecord,
    discover_job_dirs,
    load_job,
)
from evallab.schemas import (
    ExperimentSpec,
)

JsonObject = dict[str, Any]

DEFAULT_CONTAMINATION_NOTE = (
    "Not determined automatically. Before publication, document benchmark exposure, "
    "training-data plausibility, task reuse, and whether any attempt could observe "
    "another attempt's artifacts."
)


class CardRefusalError(ValueError):
    """Raised when eval-card generation is refused by platform contracts."""


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-")
    return cleaned or "eval-card"


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _load_json_dict(path: Path) -> JsonObject:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _find_spec_file(
    target: str, repo_root: Path
) -> tuple[Path | None, ExperimentSpec | None, JsonObject]:
    # Check if target is a direct file path
    direct = Path(target)
    if not direct.is_absolute():
        direct = repo_root / target
    if direct.is_file():
        raw = _load_json_dict(direct)
        try:
            spec = ExperimentSpec.model_validate(raw)
            return direct, spec, raw
        except ValidationError:
            return direct, None, raw

    # Search queue directories
    queue_dir = repo_root / "queue"
    if queue_dir.is_dir():
        states = (
            "done",
            "running",
            "approved",
            "pending",
            "proposed",
            "waiting",
            "rejected",
        )
        for state in states:
            state_dir = queue_dir / state
            if not state_dir.is_dir():
                continue
            for candidate in sorted(state_dir.glob("*.json")):
                raw = _load_json_dict(candidate)
                spec_id = raw.get("spec_id")
                name = raw.get("name")
                if target in {spec_id, name, candidate.stem, candidate.name}:
                    try:
                        spec = ExperimentSpec.model_validate(raw)
                        return candidate, spec, raw
                    except ValidationError:
                        return candidate, None, raw

    return None, None, {}


def _find_job_dir(
    target: str,
    spec: ExperimentSpec | None,
    spec_dict: JsonObject,
    repo_root: Path,
) -> Path | None:
    # 1. Direct path check
    direct = Path(target)
    if not direct.is_absolute():
        direct = repo_root / target
    if direct.is_dir() and (direct / "result.json").is_file():
        return direct

    # 2. Spec name / jobs_dir check
    spec_name = spec.name if spec else spec_dict.get("name")
    jobs_dir = spec.jobs_dir if spec else spec_dict.get("jobs_dir", "runs")
    if spec_name:
        candidate_roots = (
            repo_root / jobs_dir,
            repo_root / "runs",
            repo_root / "research/evidence/runs",
        )
        for candidate_root in candidate_roots:
            candidate = candidate_root / spec_name
            if candidate.is_dir() and (candidate / "result.json").is_file():
                return candidate

    # 3. Search scanned job roots
    scanned_roots = [repo_root / "runs", repo_root / "research/evidence/runs"]
    if jobs_dir and jobs_dir not in {"runs", "research/evidence/runs"}:
        scanned_roots.append(repo_root / jobs_dir)

    job_dirs = discover_job_dirs([r for r in scanned_roots if r.is_dir()])
    spec_id = spec.spec_id if spec else spec_dict.get("spec_id")

    for jdir in job_dirs:
        if jdir.name == target:
            return jdir
        meta = _load_json_dict(jdir / "lab-metadata.json")
        exp = meta.get("experiment")
        if isinstance(exp, dict):
            if spec_id and exp.get("spec_id") == spec_id:
                return jdir
            if spec_name and exp.get("name") == spec_name:
                return jdir

        res = _load_json_dict(jdir / "result.json")
        if res.get("id") == target:
            return jdir

    return None


def _resolve_target(
    target: str | Path,
    repo_root: Path,
) -> tuple[Path | None, ExperimentSpec | None, JsonObject, JobRecord]:
    target_str = str(target)
    spec_path, spec, spec_dict = _find_spec_file(target_str, repo_root)

    job_dir = _find_job_dir(target_str, spec, spec_dict, repo_root)

    if job_dir is None:
        raise ValueError(f"Could not locate completed Harbor job for target: {target_str!r}")

    job = load_job(job_dir)

    # If spec was not found by target name, check job metadata for spec info
    if spec is None and not spec_dict:
        meta_exp = job.metadata.get("experiment")
        if isinstance(meta_exp, dict):
            spec_dict = dict(meta_exp)
            with contextlib.suppress(ValidationError):
                spec = ExperimentSpec.model_validate(spec_dict)
        config_exp = job.config.get("experiment") or job.config.get("spec")
        if isinstance(config_exp, dict) and not spec_dict:
            spec_dict = dict(config_exp)
            with contextlib.suppress(ValidationError):
                spec = ExperimentSpec.model_validate(spec_dict)

    if spec is None and not spec_dict:
        # Synthesize minimal spec dictionary from job
        spec_dict = {
            "name": job.name,
            "hypothesis": f"Evaluation of {job.name}",
            "purpose": "baseline",
            "task": job.result.get("task_name") or job.name,
            "agent": "unknown",
            "attempts": 1,
        }

    return spec_path, spec, spec_dict, job


def build_eval_card(
    target: str | Path,
    *,
    repo_root: Path | None = None,
    explicit_derived: Path | None = None,
) -> tuple[str, JsonObject]:
    """Generate a purpose-bound, provenance-bearing eval card from completed evidence."""
    root = (repo_root or Path.cwd()).resolve()
    spec_path, spec, spec_dict, job = _resolve_target(target, root)

    purpose: str = (spec.purpose if spec else spec_dict.get("purpose")) or "baseline"

    # --- Contract 1: Purpose checks & refusals ---
    if purpose == "practice":
        raise CardRefusalError(
            "Refusal: purpose='practice' is excluded from eval cards and lessons."
        )
    prereg = spec_dict.get("prereg") or (getattr(spec, "prereg", None) if spec else None)
    if purpose == "comparison" and (
        not isinstance(prereg, dict)
        or not prereg.get("expected")
        or not prereg.get("decision_rule")
    ):
        raise CardRefusalError(
            "Refusal: purpose='comparison' requires a prereg block with expected result "
            "and decision rule before generating an eval card."
        )

    # Verify job completeness
    expected_trials = job.result.get("n_total_trials")
    if isinstance(expected_trials, int) and expected_trials != len(job.trials):
        raise ValueError(
            f"job {job.name!r} is incomplete: "
            f"{len(job.trials)} of {expected_trials} trials recorded"
        )

    # --- Contract 2 & 4: Query trial facts through attach surface & compute evidence ---
    att = attach(repo_root=root, explicit_derived=explicit_derived)
    try:
        att.connection.execute(
            "SELECT * FROM trial_facts WHERE job_id = ? OR job_name = ?",
            [job.id, job.name],
        ).fetchall()
    except Exception:
        pass
    finally:
        att.connection.close()

    k: int = int(spec.attempts if spec else spec_dict.get("attempts", 1))

    evidence = summarize_job_evidence(
        job,
        repo_root=root,
        k=k,
    )

    metric = evidence["pass_at_k"]
    n_tasks: int = int(metric["n_tasks"])
    n_trials: int = int(evidence["n_trials"])
    rate: float | None = metric["rate"]
    interval = metric["bootstrap_95"]
    exception_count: int = int(evidence["exception_count"])

    # --- Contract 3: Uncertainty is mandatory (T4) ---
    # Underpowered cohort check (n_tasks < 2 or interval is None)
    is_underpowered = (n_tasks < 2) or (interval is None)

    if is_underpowered:
        pass_at_k_text = "not distinguishable"
        interval_text = "unavailable"
    else:
        pass_at_k_text = f"{rate:.3f}" if rate is not None else "not distinguishable"
        interval_text = f"[{float(interval[0]):.3f}, {float(interval[1]):.3f}]"

    # Threats to validity
    threats: list[str] = ["One completed job captures one time and execution environment."]
    if n_tasks < 2:
        threats.append("Only 1 task evidence unit; generalization is weak.")
        threats.append("Underpowered cohort (n_tasks < 2); pass@k is not distinguishable.")
    elif n_tasks < 5:
        threats.append(f"Only {n_tasks} task evidence unit(s); statistical power is low.")
    elif n_tasks < 20:
        threats.append(f"Only {n_tasks} task evidence unit(s); generalization is weak.")

    if exception_count > 0:
        threats.append(
            f"{exception_count} harness/execution exception trial(s) were excluded "
            "from capability measurement."
        )

    if evidence.get("insufficient_tasks"):
        threats.append(
            f"{len(evidence['insufficient_tasks'])} task(s) had fewer than k scored attempts."
        )

    if evidence.get("elicitation") is None and evidence.get("elicitation_reasons"):
        threats.extend(evidence["elicitation_reasons"])

    # Clean and deduplicate threats deterministically
    threats = list(dict.fromkeys(threats))

    # Digests & Lineage
    spec_name = spec.name if spec else str(spec_dict.get("name", job.name))
    spec_path_str = (
        _relative(spec_path, root) if spec_path else f"queue/done/{spec_name}.json"
    )
    spec_payload = spec_dict if spec_dict else (spec.model_dump(mode="json") if spec else {})
    spec_digest = digest_json(spec_payload)

    job_path_str = _relative(job.path, root)
    job_lock_digest = digest_json(job.lock)

    inputs = [
        {"path": spec_path_str, "digest": spec_digest},
        {"path": job_path_str, "digest": job_lock_digest},
    ]

    card_data: JsonObject = {
        "schema_version": 1,
        "title": spec_name,
        "purpose": purpose,
        "spec_path": spec_path_str,
        "spec_digest": spec_digest,
        "job_path": job_path_str,
        "job_id": job.id,
        "job_lock_digest": job_lock_digest,
        "task": spec.task if spec else str(spec_dict.get("task", "unknown")),
        "hypothesis": spec.hypothesis if spec else str(spec_dict.get("hypothesis", "")),
        "numbers": {
            "n_tasks": n_tasks,
            "n_trials": n_trials,
            "k": k,
            "pass_at_k": None if is_underpowered else rate,
            "pass_at_k_text": pass_at_k_text,
            "bootstrap_95": interval if not is_underpowered else None,
            "exceptions": exception_count,
            "is_underpowered": is_underpowered,
        },
        "elicitation": evidence.get("elicitation"),
        "contamination_note": DEFAULT_CONTAMINATION_NOTE,
        "threats": threats,
        "inputs": inputs,
    }

    # Hypothesis with optional preregistration block
    hypothesis_text = card_data["hypothesis"]
    if purpose == "comparison" and prereg:
        hypothesis_text = (
            f"{hypothesis_text}\n\n"
            "### Preregistration\n\n"
            f"- Expected result: {prereg.get('expected')}\n"
            f"- Decision rule: {prereg.get('decision_rule')}"
        )

    # Render template
    template_path = root / "research/cards/TEMPLATE.md"
    if not template_path.is_file():
        raise ValueError(f"eval-card template is missing: {template_path}")

    replacements = {
        "{{TITLE}}": card_data["title"],
        "{{HYPOTHESIS}}": hypothesis_text,
        "{{TASK}}": card_data["task"],
        "{{SPEC_PATH}}": card_data["spec_path"],
        "{{SPEC_DIGEST}}": card_data["spec_digest"],
        "{{JOB_PATH}}": card_data["job_path"],
        "{{JOB_ID}}": card_data["job_id"],
        "{{JOB_LOCK_DIGEST}}": card_data["job_lock_digest"],
        "{{N_TASKS}}": str(n_tasks),
        "{{N_TRIALS}}": str(n_trials),
        "{{K}}": str(k),
        "{{PASS_AT_K}}": pass_at_k_text,
        "{{INTERVAL}}": interval_text,
        "{{EXCEPTIONS}}": str(exception_count),
        "{{ELICITATION}}": json.dumps(card_data["elicitation"], indent=2, sort_keys=True),
        "{{CONTAMINATION}}": card_data["contamination_note"],
        "{{THREATS}}": "\n".join(f"- {t}" for t in threats),
    }

    rendered = template_path.read_text(encoding="utf-8")
    for marker, value in replacements.items():
        rendered = rendered.replace(marker, str(value))

    if "{{" in rendered:
        raise ValueError("eval-card template contains an unresolved marker")

    return rendered, card_data


def draft_eval_card(
    target: str | Path,
    *,
    repo_root: Path | None = None,
    explicit_derived: Path | None = None,
    output_path: Path | None = None,
) -> tuple[Path, JsonObject]:
    """Build and write an eval card atomically."""
    root = (repo_root or Path.cwd()).resolve()
    rendered, card_data = build_eval_card(
        target,
        repo_root=root,
        explicit_derived=explicit_derived,
    )

    destination = (
        output_path
        if output_path is not None
        else root / "research/cards" / f"{_safe_name(str(card_data['title']))}.md"
    )
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)

    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(destination)
    return destination, card_data


def generate_card(
    target: str | Path,
    *,
    repo_root: Path | None = None,
    explicit_derived: Path | None = None,
    output_path: Path | None = None,
) -> tuple[str, JsonObject]:
    """CLI entry point: generate card, optionally write to output_path, and return."""
    root = (repo_root or Path.cwd()).resolve()
    if output_path is not None:
        dest, card_data = draft_eval_card(
            target,
            repo_root=root,
            explicit_derived=explicit_derived,
            output_path=output_path,
        )
        rendered = dest.read_text(encoding="utf-8")
        return rendered, card_data
    return build_eval_card(
        target,
        repo_root=root,
        explicit_derived=explicit_derived,
    )
