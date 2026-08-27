from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from evallab.cohort import TIMEOUT_BUDGET_EXCEPTION_CLASSES, compare
from evallab.evidence.facts import digest_json
from evallab.schemas import (
    CapabilityCurveReport,
    CapabilityCurveSpec,
    CurveComparisonSource,
    CurveContrastReport,
    CurveExceptionReport,
    CurveLevelReport,
    CurveMetricReport,
)

JsonObject = dict[str, Any]


def load_curve_spec(path: Path) -> CapabilityCurveSpec:
    try:
        return CapabilityCurveSpec.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise ValueError(f"invalid capability curve spec {path}: {exc}") from exc


def load_curve_report(path: Path) -> CapabilityCurveReport:
    try:
        return CapabilityCurveReport.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise ValueError(f"invalid capability curve report {path}: {exc}") from exc


def _sha256_bytes(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _safe_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if path != root.resolve() and root.resolve() not in path.parents:
        raise ValueError(f"curve input escapes repository: {relative}")
    return path


def _comparison_report(
    source: CurveComparisonSource, *, repo_root: Path
) -> tuple[JsonObject, dict[str, str]]:
    if source.comparison_spec is not None:
        report = compare(source.comparison_spec, repo_root=repo_root)
        digests = {
            f"comparison:{json.dumps(source.level, sort_keys=True)}:spec": digest_json(
                source.comparison_spec.model_dump(mode="json")
            )
        }
        for selector in source.comparison_spec.cohorts:
            for value in selector.paths:
                selected = _safe_path(repo_root, value)
                if not selected.exists():
                    raise ValueError(f"curve cohort input does not exist: {value}")
                files = [selected] if selected.is_file() else sorted(selected.rglob("*.json"))
                for path in files:
                    relative = path.resolve().relative_to(repo_root.resolve()).as_posix()
                    digests[relative] = _sha256_bytes(path.read_bytes())
        return report, digests

    assert source.comparison_artifact is not None
    path = _safe_path(repo_root, source.comparison_artifact)
    content = path.read_bytes()
    observed = _sha256_bytes(content)
    if observed != source.comparison_artifact_digest:
        raise ValueError(
            f"frozen comparison artifact digest mismatch for {source.comparison_artifact}: "
            f"expected {source.comparison_artifact_digest}, observed {observed}"
        )
    try:
        report = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid frozen comparison artifact {path}: {exc}") from exc
    if not isinstance(report, dict):
        raise ValueError(f"frozen comparison artifact is not an object: {path}")
    return report, {source.comparison_artifact: observed}


def _metric(value: JsonObject) -> CurveMetricReport:
    selected = value.get("selected_trials")
    selected_tasks = sorted(selected) if isinstance(selected, dict) else []
    return CurveMetricReport(
        k=int(value["k"]),
        n_tasks=int(value["n_tasks"]),
        rate=value.get("rate"),
        task_interval_95=value.get("bootstrap_95"),
        passes=int(value["passes"]),
        selected_task_blocks=selected_tasks,
        insufficient_task_blocks=sorted(value.get("insufficient_attempt_groups") or []),
    )


def _members_by_label(report: JsonObject) -> dict[str, list[JsonObject]]:
    result: dict[str, list[JsonObject]] = {}
    for cohort in report.get("cohorts", []):
        if isinstance(cohort, dict) and isinstance(cohort.get("members"), list):
            result[str(cohort["label"])] = [
                member for member in cohort["members"] if isinstance(member, dict)
            ]
    return result


def _json_mapping(value: object) -> JsonObject | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _factor_integrity_reasons(
    *,
    members: list[JsonObject],
    pair_set: list[str],
    cohort_label: str,
    expected_level: object,
    spec: CapabilityCurveSpec,
) -> list[str]:
    selected = [member for member in members if member.get("task_block_id") in pair_set]
    prefix = f"cohort {cohort_label!r}"
    if not selected:
        return [f"{prefix} has no paired members for factor provenance"]
    coordinates = [_json_mapping(member.get("factor_values_json")) for member in selected]
    if any(coordinate is None for coordinate in coordinates):
        return [f"{prefix} has missing or invalid factor_values_json"]
    unique = {
        json.dumps(coordinate, sort_keys=True, separators=(",", ":"))
        for coordinate in coordinates
        if coordinate is not None
    }
    if len(unique) != 1:
        return [f"{prefix} mixes {len(unique)} authoritative factor coordinates"]
    coordinate = json.loads(next(iter(unique)))
    if spec.factor_name not in coordinate:
        return [f"{prefix} factor coordinate omits {spec.factor_name!r}"]
    if json.dumps(coordinate[spec.factor_name], sort_keys=True) != json.dumps(
        expected_level, sort_keys=True
    ):
        return [
            f"{prefix} factor {spec.factor_name!r} is "
            f"{coordinate[spec.factor_name]!r}, expected {expected_level!r}"
        ]

    reasons: list[str] = []
    if spec.factor_kind == "task_generator":
        if any(
            member.get("task_instance_id") is None or member.get("generator_seed_json") is None
            for member in selected
        ):
            reasons.append(f"{prefix} task-generator factor lacks instance/seed provenance")
        for member in selected:
            bindings = _json_mapping(member.get("factor_bindings_json"))
            if bindings is None:
                reasons.append(f"{prefix} has missing factor binding provenance")
                break
            if spec.factor_name in bindings:
                reasons.append(f"{prefix} task-generator factor falsely claims harness execution")
                break
        return reasons

    assert spec.treatment_binding is not None
    for member in selected:
        bindings = _json_mapping(member.get("factor_bindings_json"))
        if bindings is None or bindings.get(spec.factor_name) != spec.treatment_binding:
            reasons.append(f"{prefix} execution factor is not bound to {spec.treatment_binding!r}")
            break
    for member in selected:
        bound = _json_mapping(member.get("bound_execution_values_json"))
        if bound is None or json.dumps(
            bound.get(spec.treatment_binding), sort_keys=True
        ) != json.dumps(expected_level, sort_keys=True):
            reasons.append(
                f"{prefix} bound execution value does not match factor level {expected_level!r}"
            )
            break
    return reasons


def _controlled_tuple(member: JsonObject, treatment_binding: str | None) -> JsonObject:
    bound_values: JsonObject | None = None
    raw_bound = member.get("bound_execution_values_json")
    if isinstance(raw_bound, str):
        value = json.loads(raw_bound)
        if isinstance(value, dict):
            bound_values = dict(value)
            if treatment_binding is not None:
                bound_values.pop(treatment_binding, None)
    return {
        "task_block_id": member.get("task_block_id"),
        "task_block_inputs_json": member.get("task_block_inputs_json"),
        "agent_name": member.get("agent_name"),
        "agent_version": member.get("agent_version"),
        "model_name": member.get("model_name"),
        "model_settings_digest": member.get("model_settings_digest"),
        "preamble_hash": member.get("preamble_hash"),
        "preamble_content_sha256": member.get("preamble_content_sha256"),
        "toolset_digest": member.get("toolset_digest"),
        "environment_digest": member.get("environment_digest"),
        "harness_policy_digest": member.get("harness_policy_digest"),
        "simulator_digest": member.get("simulator_digest"),
        "verifier_digest": member.get("verifier_digest"),
        "factor_bindings_digest": member.get("factor_bindings_digest"),
        "bound_execution_values": bound_values,
    }


def _budget_failure(member: JsonObject, enabled: bool) -> bool:
    return bool(enabled and member.get("exception_class") in TIMEOUT_BUDGET_EXCEPTION_CLASSES)


def _control_fingerprint(
    members: list[JsonObject],
    pair_set: list[str],
    treatment_binding: str | None,
    *,
    budget_exhaustion_is_failure: bool,
) -> str:
    selected = [
        _controlled_tuple(member, treatment_binding)
        for member in members
        if member.get("task_block_id") in pair_set
        and (
            (member.get("exception_class") is None and member.get("reward") is not None)
            or _budget_failure(member, budget_exhaustion_is_failure)
        )
    ]
    selected.sort(key=lambda item: json.dumps(item, sort_keys=True))
    return digest_json(selected)


def _exclusions(
    members: list[JsonObject], *, budget_exhaustion_is_failure: bool
) -> tuple[list[CurveExceptionReport], list[str], list[str]]:
    exceptions: list[CurveExceptionReport] = []
    missing_rewards: list[str] = []
    censored: set[str] = set()
    for member in members:
        exception = member.get("exception_class")
        is_budget_failure = _budget_failure(member, budget_exhaustion_is_failure)
        if exception is not None and not is_budget_failure:
            exceptions.append(
                CurveExceptionReport(
                    trial_id=str(member["trial_id"]),
                    task_block_id=(
                        str(member["task_block_id"])
                        if member.get("task_block_id") is not None
                        else None
                    ),
                    exception_class=str(exception),
                )
            )
            if member.get("task_block_id") is not None:
                censored.add(str(member["task_block_id"]))
        elif member.get("reward") is None and not is_budget_failure:
            missing_rewards.append(str(member["trial_id"]))
            if member.get("task_block_id") is not None:
                censored.add(str(member["task_block_id"]))
    return (
        sorted(exceptions, key=lambda item: item.trial_id),
        sorted(missing_rewards),
        sorted(censored),
    )


def _contrast(
    paired: JsonObject, *, primary: bool, integrity_reasons: list[str]
) -> CurveContrastReport:
    reasons = list(paired.get("refusal_reasons") or [])
    reasons.extend(integrity_reasons)
    rankable = bool(paired.get("rankable")) and not integrity_reasons if primary else False
    if not primary:
        reasons.append("descriptive level; not preregistered primary contrast")
    reasons = list(dict.fromkeys(reasons))
    return CurveContrastReport(
        k=int(paired["k"]),
        n_pairs=int(paired["n_pairs"]),
        paired_delta=paired.get("mean_pass_any_first_k_delta"),
        paired_interval_95=paired.get("bootstrap_95"),
        wins=int(paired.get("wins", 0)),
        ties=int(paired.get("ties", 0)),
        losses=int(paired.get("losses", 0)),
        pass_all_first_k_delta=paired.get("mean_pass_all_first_k_delta"),
        pass_all_first_k_interval_95=paired.get("pass_all_first_k_bootstrap_95"),
        pass_all_first_k_wins=int(paired.get("pass_all_first_k_wins", 0)),
        pass_all_first_k_ties=int(paired.get("pass_all_first_k_ties", 0)),
        pass_all_first_k_losses=int(paired.get("pass_all_first_k_losses", 0)),
        rankable=rankable,
        refusal_reasons=reasons,
    )


def build_curve(
    spec: CapabilityCurveSpec,
    *,
    repo_root: Path,
    produced_by: str,
    produced_at: datetime | None = None,
) -> CapabilityCurveReport:
    reports: list[tuple[CurveComparisonSource, JsonObject]] = []
    input_digests = {"$curve_spec": digest_json(spec.model_dump(mode="json"))}
    for source in spec.comparisons:
        report, source_digests = _comparison_report(source, repo_root=repo_root)
        reports.append((source, report))
        for path, digest in source_digests.items():
            previous = input_digests.get(path)
            if previous is not None and previous != digest:
                raise ValueError(f"curve input changed while reading: {path}")
            input_digests[path] = digest

    reasons: list[str] = []
    expected_pair_set: list[str] | None = None
    expected_control: str | None = None
    reference_metrics: tuple[list[CurveMetricReport], list[CurveMetricReport]] | None = None
    reference_exclusions: tuple[list[CurveExceptionReport], list[str], list[str]] | None = None
    reference_unpaired: set[str] = set()
    built_levels: dict[str, CurveLevelReport] = {}

    for source, report in reports:
        if report.get("pairing_key") != "task_block_id":
            reasons.append(f"level {source.level!r} does not use task_block_id pairing")
        cohorts = report.get("cohorts")
        paired_rows = report.get("paired")
        if not isinstance(cohorts, list) or len(cohorts) != 2 or not isinstance(paired_rows, list):
            raise ValueError(
                f"comparison for level {source.level!r} is not a two-arm cohort report"
            )
        baseline = cohorts[0]
        level_cohort = cohorts[1]
        if not isinstance(baseline, dict) or not isinstance(level_cohort, dict):
            raise ValueError(f"comparison for level {source.level!r} has malformed cohorts")
        if "pass_all_first_k" not in baseline or "pass_all_first_k" not in level_cohort:
            raise ValueError(
                f"comparison for level {source.level!r} lacks pass-all-first-k metrics"
            )

        pair_sets = [
            sorted(str(pair["key"]) for pair in row.get("pairs", [])) for row in paired_rows
        ]
        if not pair_sets:
            pair_sets = [[]]
        if any(pair_set != pair_sets[0] for pair_set in pair_sets[1:]):
            reasons.append(f"level {source.level!r} does not have one exact pair set across k")
        pair_set = pair_sets[0]
        if expected_pair_set is None:
            expected_pair_set = pair_set
        elif pair_set != expected_pair_set:
            reasons.append(f"level {source.level!r} does not share the common exact pair set")

        members = _members_by_label(report)
        baseline_label = str(baseline["label"])
        level_label = str(level_cohort["label"])
        level_integrity_reasons: list[str] = []
        if json.dumps(source.level, sort_keys=True) == json.dumps(
            spec.reference_level, sort_keys=True
        ):
            level_integrity_reasons.append(
                f"level {source.level!r} does not differ from reference level"
            )
        level_integrity_reasons.extend(
            _factor_integrity_reasons(
                members=members.get(baseline_label, []),
                pair_set=pair_set,
                cohort_label=baseline_label,
                expected_level=spec.reference_level,
                spec=spec,
            )
        )
        level_integrity_reasons.extend(
            _factor_integrity_reasons(
                members=members.get(level_label, []),
                pair_set=pair_set,
                cohort_label=level_label,
                expected_level=source.level,
                spec=spec,
            )
        )
        reasons.extend(level_integrity_reasons)
        budget_failure = bool(report.get("budget_exhaustion_is_failure", False))
        baseline_control = _control_fingerprint(
            members.get(baseline_label, []),
            pair_set,
            spec.treatment_binding,
            budget_exhaustion_is_failure=budget_failure,
        )
        level_control = _control_fingerprint(
            members.get(level_label, []),
            pair_set,
            spec.treatment_binding,
            budget_exhaustion_is_failure=budget_failure,
        )
        if baseline_control != level_control:
            reasons.append(f"level {source.level!r} has a controlled fingerprint mismatch")
        if expected_control is None:
            expected_control = baseline_control
        elif baseline_control != expected_control:
            reasons.append(
                f"level {source.level!r} does not share the common controlled fingerprint"
            )

        if budget_failure != (spec.treatment_binding == "timeout_seconds"):
            reasons.append(
                f"level {source.level!r} budget exhaustion policy does not match the treatment"
            )
        base_exclusions = _exclusions(
            members.get(baseline_label, []), budget_exhaustion_is_failure=budget_failure
        )
        level_exclusions = _exclusions(
            members.get(level_label, []), budget_exhaustion_is_failure=budget_failure
        )
        base_exclusions = (
            base_exclusions[0],
            base_exclusions[1],
            sorted(set(base_exclusions[2]) - set(pair_set)),
        )
        level_exclusions = (
            level_exclusions[0],
            level_exclusions[1],
            sorted(set(level_exclusions[2]) - set(pair_set)),
        )
        if reference_exclusions is None:
            reference_exclusions = base_exclusions
        elif base_exclusions != reference_exclusions:
            reasons.append(f"level {source.level!r} changes reference censoring/exceptions")

        pass_any = [_metric(item) for item in level_cohort["pass_any_first_k"]]
        pass_all = [_metric(item) for item in level_cohort["pass_all_first_k"]]
        base_pass_any = [_metric(item) for item in baseline["pass_any_first_k"]]
        base_pass_all = [_metric(item) for item in baseline["pass_all_first_k"]]
        current_reference = (base_pass_any, base_pass_all)
        if reference_metrics is None:
            reference_metrics = current_reference
        elif current_reference != reference_metrics:
            reasons.append(f"level {source.level!r} changes reference metrics")

        primary_level = source.level == spec.primary_contrast.level
        contrasts = [
            _contrast(
                row,
                primary=(primary_level and int(row["k"]) == spec.primary_contrast.k),
                integrity_reasons=level_integrity_reasons,
            )
            for row in paired_rows
        ]
        unpaired = sorted(
            {str(value) for row in paired_rows for value in row.get("unpaired_tasks", [])}
        )
        if unpaired:
            reasons.append(
                f"level {source.level!r} has {len(unpaired)} unpaired eligible task block(s)"
            )
        for row in paired_rows:
            if int(row.get("n_pairs", 0)) < 2:
                reasons.append(f"level {source.level!r} k={row.get('k')} has fewer than 2 pairs")
            for reason in row.get("refusal_reasons", []):
                text = str(reason)
                if any(
                    marker in text
                    for marker in (
                        "fewer than k",
                        "lack task identity",
                        "missing ",
                        " mixes ",
                        "undeclared consequential",
                        "controlled factor provenance",
                        "not paired across cohorts",
                        "first-k order is unavailable",
                        "missing or invalid started_at",
                        "started_at tie straddles first-k boundary",
                    )
                ):
                    reasons.append(f"level {source.level!r}: {text}")
        reference_unpaired.update(unpaired)
        exception_trials, missing_trials, censored = level_exclusions
        role = "primary" if primary_level else "descriptive"
        built_levels[json.dumps(source.level, sort_keys=True)] = CurveLevelReport(
            level=source.level,
            role=role,
            exact_pair_set=pair_set,
            unpaired_task_blocks=unpaired,
            censored_task_blocks=censored,
            exception_trials=exception_trials,
            missing_reward_trials=missing_trials,
            pass_any_first_k=pass_any,
            pass_all_first_k=pass_all,
            contrasts=contrasts,
        )

    assert reference_metrics is not None and reference_exclusions is not None
    ref_exception, ref_missing, ref_censored = reference_exclusions
    reference_report = CurveLevelReport(
        level=spec.reference_level,
        role="reference",
        exact_pair_set=expected_pair_set or [],
        unpaired_task_blocks=sorted(reference_unpaired),
        censored_task_blocks=ref_censored,
        exception_trials=ref_exception,
        missing_reward_trials=ref_missing,
        pass_any_first_k=reference_metrics[0],
        pass_all_first_k=reference_metrics[1],
        contrasts=[],
    )
    built_levels[json.dumps(spec.reference_level, sort_keys=True)] = reference_report

    primary = built_levels[json.dumps(spec.primary_contrast.level, sort_keys=True)]
    primary_result = next(item for item in primary.contrasts if item.k == spec.primary_contrast.k)
    reasons.extend(primary_result.refusal_reasons)
    reasons = list(dict.fromkeys(reasons))
    levels = [built_levels[json.dumps(level, sort_keys=True)] for level in spec.ordered_levels]
    return CapabilityCurveReport(
        curve_id=spec.curve_id,
        factor_name=spec.factor_name,
        factor_unit=spec.factor_unit,
        factor_kind=spec.factor_kind,
        ordered_levels=spec.ordered_levels,
        reference_level=spec.reference_level,
        primary_contrast=spec.primary_contrast,
        prereg=spec.prereg,
        common_controlled_fingerprint=(
            None if any("fingerprint" in reason for reason in reasons) else expected_control
        ),
        input_digests=dict(sorted(input_digests.items())),
        produced_at=produced_at or datetime.now(UTC),
        produced_by=produced_by,
        rankable=primary_result.rankable and not reasons,
        refuse_to_rank_reasons=reasons,
        levels=levels,
    )


def write_curve(
    spec_path: Path,
    *,
    repo_root: Path,
    output_path: Path,
    produced_by: str,
) -> tuple[Path, CapabilityCurveReport]:
    spec = load_curve_spec(spec_path)
    report = build_curve(spec, repo_root=repo_root, produced_by=produced_by)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite capability curve: {output_path}")
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temporary.replace(output_path)
    return output_path, report
