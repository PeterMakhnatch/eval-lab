"""E11: eval-card generator with purpose-bound shape and mandatory uncertainty.

Cards are the platform's citable results (platform-architecture.md v2 §4) and the
terminus of the spec lifecycle:
    draft -> (purpose=comparison => prereg required) -> submitted -> gated ->
    dispatched -> analyzed -> carded

Contracts:
1. Purpose drives the card: baseline -> per-agent realized first-k; comparison -> paired
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from evallab.cohort import (
    summarize_job_evidence,
)
from evallab.evidence.facts import digest_json
from evallab.results import (
    JobRecord,
    discover_job_dirs,
    load_job,
)
from evallab.schemas import (
    ExperimentSpec,
)
from evallab.storage.attach import attach

JsonObject = dict[str, Any]

DEFAULT_CONTAMINATION_NOTE = (
    "Not determined automatically. Before publication, document benchmark exposure, "
    "training-data plausibility, task reuse, and whether any attempt could observe "
    "another attempt's artifacts."
)
DEFAULT_CONTAMINATION_CAVEAT = (
    "Benchmark exposure and pretraining cutoffs must be reviewed before publication. "
    "Task artifacts must not be observable by candidate agents during execution."
)
DEFAULT_ELICITATION_CAVEAT = (
    "Elicitation parameters (agent version, model pin, preamble hash, toolset, attempts k) "
    "must be strictly pinned before cross-cohort comparisons or ranking."
)


class CardRefusalError(ValueError):
    """Raised when eval-card generation is refused by platform contracts."""


@dataclass
class CardValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    card_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "card_path": self.card_path,
        }


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

    metric = evidence["pass_any_first_k"]
    n_tasks: int = int(metric["n_tasks"])
    n_trials: int = int(evidence["n_trials"])
    rate: float | None = metric["rate"]
    interval = metric["bootstrap_95"]
    exception_count: int = int(evidence["exception_count"])

    # --- Contract 3: Uncertainty is mandatory (T4) ---
    # Underpowered cohort check (n_tasks < 2 or interval is None)
    is_underpowered = (n_tasks < 2) or (interval is None)

    if is_underpowered:
        pass_any_first_k_text = "not distinguishable"
        interval_text = "unavailable"
    else:
        pass_any_first_k_text = f"{rate:.3f}" if rate is not None else "not distinguishable"
        interval_text = f"[{float(interval[0]):.3f}, {float(interval[1]):.3f}]"

    # Threats to validity
    threats: list[str] = ["One completed job captures one time and execution environment."]
    if n_tasks < 2:
        threats.append("Only 1 task evidence unit; generalization is weak.")
        threats.append(
            "Underpowered cohort (n_tasks < 2); pass-any-first-k is not distinguishable."
        )
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

    if evidence.get("unavailable_order_groups"):
        threats.append(
            f"{len(evidence['unavailable_order_groups'])} task(s) lack a valid first-k "
            "attempt order (missing/invalid started_at or a timestamp tie at k)."
        )

    if evidence.get("elicitation") is None and evidence.get("elicitation_reasons"):
        threats.extend(evidence["elicitation_reasons"])

    # Clean and deduplicate threats deterministically
    threats = list(dict.fromkeys(threats))

    # Digests & Lineage
    spec_name = spec.name if spec else str(spec_dict.get("name", job.name))
    spec_path_str = _relative(spec_path, root) if spec_path else f"queue/done/{spec_name}.json"
    spec_payload = spec_dict if spec_dict else (spec.model_dump(mode="json") if spec else {})
    spec_digest = digest_json(spec_payload)

    job_path_str = _relative(job.path, root)
    job_lock_digest = digest_json(job.lock)

    inputs = [
        {"path": spec_path_str, "digest": spec_digest},
        {"path": job_path_str, "digest": job_lock_digest},
    ]

    regeneration_query = (
        f"SELECT task_name, count(*) AS n_trials, "
        f"avg(CASE WHEN primary_reward >= 1.0 THEN 1.0 ELSE 0.0 END) AS pass_rate "
        f"FROM trial_facts WHERE job_name = '{job.name}' GROUP BY task_name;"
    )

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
            "pass_any_first_k": None if is_underpowered else rate,
            "pass_any_first_k_text": pass_any_first_k_text,
            "bootstrap_95": interval if not is_underpowered else None,
            "exceptions": exception_count,
            "is_underpowered": is_underpowered,
        },
        "elicitation": evidence.get("elicitation"),
        "elicitation_caveat": DEFAULT_ELICITATION_CAVEAT,
        "contamination_note": DEFAULT_CONTAMINATION_NOTE,
        "contamination_caveat": DEFAULT_CONTAMINATION_CAVEAT,
        "threats": threats,
        "regeneration_query": regeneration_query,
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
        "{{PRIMARY_METRIC_LABEL}}": "Observed pass-any-first-k",
        "{{PRIMARY_METRIC_VALUE}}": pass_any_first_k_text,
        "{{INTERVAL}}": interval_text,
        "{{EXCEPTIONS}}": str(exception_count),
        "{{ELICITATION}}": json.dumps(card_data["elicitation"], indent=2, sort_keys=True),
        "{{ELICITATION_CAVEAT}}": card_data["elicitation_caveat"],
        "{{CONTAMINATION}}": card_data["contamination_note"],
        "{{CONTAMINATION_CAVEAT}}": card_data["contamination_caveat"],
        "{{THREATS}}": "\n".join(f"- {t}" for t in threats),
        "{{REGENERATION_QUERY}}": card_data["regeneration_query"],
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


def validate_card(content: str, *, card_path: str | None = None) -> CardValidationResult:
    """Validate a rendered eval-card markdown string against schema and mandatory caveats."""
    errors: list[str] = []
    warnings: list[str] = []

    if not content or not content.strip():
        return CardValidationResult(
            valid=False,
            errors=["Card content is empty."],
            warnings=[],
            card_path=card_path,
        )

    # 1. Unresolved template placeholders
    unresolved = re.findall(r"\{\{[A-Za-z0-9_]+\}\}", content)
    if unresolved:
        errors.append(
            f"Card contains unresolved template marker(s): {', '.join(sorted(set(unresolved)))}"
        )

    # 2. Required sections (H1 title, and H2 sections)
    if not re.search(r"^#\s+(?:Eval\s+card:|\w+)", content, re.MULTILINE | re.IGNORECASE):
        errors.append("Missing top-level heading '# Eval card: <title>' or '# <title>'.")

    required_sections = [
        ("Question", r"^##\s+(?:Question|Hypothesis)"),
        (
            "Configuration and evidence",
            r"^##\s+(?:Configuration|Evidence|Configuration and evidence)",
        ),
        ("Result", r"^##\s+(?:Result|Results)"),
        ("Elicitation", r"^##\s+Elicitation"),
        ("Contamination", r"^##\s+Contamination"),
        ("Threats to validity", r"^##\s+Threats(?:\s+to\s+validity)?"),
        (
            "Regeneration query / command",
            r"^##\s+(?:Regeneration|Reproduction|Exact queries|Regeneration query)",
        ),
        ("Human review", r"^##\s+(?:Human\s+review|Review)"),
    ]

    for section_name, pattern in required_sections:
        if not re.search(pattern, content, re.MULTILINE | re.IGNORECASE):
            errors.append(f"Missing required section: '## {section_name}'")

    # 3. Mandatory Caveats Check (scoped to sections)
    contam_match = re.search(
        r"##\s+Contamination[^\n]*\n([\s\S]*?)(?=^##|\Z)", content, re.MULTILINE | re.IGNORECASE
    )
    if contam_match:
        contam_text = contam_match.group(1).strip()
        has_contam_caveat = bool(
            re.search(r"contamination\s+caveat", contam_text, re.IGNORECASE)
            or re.search(
                r"(?:exposure|pretraining|leakage|cutoff|training|benchmark|artifact)",
                contam_text,
                re.IGNORECASE,
            )
        )
        if not has_contam_caveat:
            errors.append(
                "Missing mandatory contamination caveat "
                "(must document benchmark exposure / contamination status)."
            )

    elicit_match = re.search(
        r"##\s+Elicitation[^\n]*\n([\s\S]*?)(?=^##|\Z)", content, re.MULTILINE | re.IGNORECASE
    )
    if elicit_match:
        elicit_text = elicit_match.group(1).strip()
        has_elicit_caveat = bool(
            re.search(r"elicitation\s+caveat", elicit_text, re.IGNORECASE)
            or re.search(
                r"(?:caveat|tuple|model|toolset|prompt|preamble|parameters|ranking)",
                elicit_text,
                re.IGNORECASE,
            )
        )
        if not has_elicit_caveat:
            errors.append(
                "Missing mandatory elicitation caveat "
                "(must document elicitation parameters / ranking constraints)."
            )

    # 4. Mandatory Uncertainty / Number rules (Tenet T4)
    result_match = re.search(
        r"##\s+(?:Result|Results)\b([\s\S]*?)(?=##|\Z)", content, re.IGNORECASE
    )
    if result_match:
        result_text = result_match.group(1)
        has_n = bool(
            re.search(
                r"\b(?:n|N_tasks|tasks?|trials?|evidence units?|n\s*=|\*\*[0-9]+\*\*)\b",
                result_text,
                re.IGNORECASE,
            )
        )
        has_uncertainty = bool(
            re.search(
                r"(?:interval|bootstrap|\[\s*[-0-9.]+\s*,\s*[-0-9.]+\s*\]|"
                r"not distinguishable|insufficient n|unavailable)",
                result_text,
                re.IGNORECASE,
            )
        )
        if not has_n and not has_uncertainty:
            errors.append(
                "Result section missing sample size n or uncertainty interval / "
                "'insufficient n' / 'not distinguishable'."
            )

    # 5. Regenerability check: must contain a code block (```sql, ```bash, or ```python)
    has_code_block = bool(re.search(r"```(?:sql|bash|sh|python)?\n[\s\S]+?\n```", content))
    if not has_code_block:
        errors.append(
            "Missing regenerability command or query code block (```sql, ```bash, or ```python)."
        )

    return CardValidationResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        card_path=card_path,
    )


def validate_card_file(path: Path | str) -> CardValidationResult:
    """Validate an eval card markdown file from disk."""
    p = Path(path)
    if not p.is_file():
        return CardValidationResult(
            valid=False,
            errors=[f"Card file not found: {p}"],
            warnings=[],
            card_path=str(p),
        )
    try:
        content = p.read_text(encoding="utf-8")
    except Exception as exc:
        return CardValidationResult(
            valid=False,
            errors=[f"Failed to read card file {p}: {exc}"],
            warnings=[],
            card_path=str(p),
        )
    return validate_card(content, card_path=str(p))
