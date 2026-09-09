"""HAR-13 descriptive analysis over the existing CohortComparisonSpec contract.

Run this module as a script from an installed Eval Lab checkout. It never submits
experiments or modifies source runs. JSON/Markdown/SVG share one immutable output
publication; fixture and historical results are not live improvements.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from evallab.schemas import CohortComparisonSpec

from .collector import collect_cohort_trials, safe_finite_float, safe_repo_path
from .pairing import evaluate_pairs, summarize_cohort
from .renderer import render_markdown_report, render_svg_plot

VALID_EVIDENCE_KINDS = frozenset({"fixture", "historical", "model-run"})


def _analysis_identity() -> dict[str, Any]:
    directory = Path(__file__).resolve().parent
    process = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=directory,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    return {
        "repository_revision": process.stdout.strip() if process.returncode == 0 else None,
        "source_sha256": {
            name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
            for name in ("analysis.py", "collector.py", "pairing.py", "renderer.py")
        },
    }


def _load_spec(spec: CohortComparisonSpec | Path | str | dict[str, Any]) -> CohortComparisonSpec:
    if isinstance(spec, CohortComparisonSpec):
        return spec
    if isinstance(spec, dict):
        return CohortComparisonSpec.model_validate(spec)
    if isinstance(spec, Path):
        return CohortComparisonSpec.model_validate_json(spec.read_text())
    if isinstance(spec, str) and spec.lstrip().startswith("{"):
        return CohortComparisonSpec.model_validate_json(spec)
    return CohortComparisonSpec.model_validate_json(Path(spec).read_text())


def _input_manifest(root: Path, trials: list[Any]) -> dict[str, str]:
    paths: set[Path] = set()
    for trial in trials:
        directory = safe_repo_path(root, trial.source_path)
        paths.update(directory.glob("*.json"))
        if (directory / "agent").is_dir() and not (directory / "agent").is_symlink():
            paths.update((directory / "agent").glob("*.json"))
            paths.add(directory / "agent/rlm/root-messages.json")
        for name in ("result.json", "config.json", "lock.json", "lab-metadata.json"):
            paths.add(directory.parent / name)
    result = {}
    for path in sorted(paths):
        if path.is_file() and not path.is_symlink():
            safe = safe_repo_path(root, str(path.relative_to(root)))
            result[str(safe.relative_to(root))] = hashlib.sha256(safe.read_bytes()).hexdigest()
    return result


def analyze(
    spec: CohortComparisonSpec | Path | str | dict[str, Any],
    *,
    repo_root: Path | str,
    evidence_kind: str,
) -> dict[str, Any]:
    if evidence_kind not in VALID_EVIDENCE_KINDS:
        raise ValueError(f"Invalid evidence_kind {evidence_kind!r}")
    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise ValueError(f"repo_root is not a directory: {root}")
    specification = _load_spec(spec)
    if len(specification.cohorts) != 2:
        raise ValueError("CohortComparisonSpec must contain exactly two cohorts")
    if specification.pass_k != [1]:
        raise ValueError("HAR-13 is a one-attempt descriptive comparison; pass_k must be [1]")
    if safe_finite_float("pass_threshold", specification.pass_threshold, []) is None:
        raise ValueError("pass_threshold must be a finite number")
    baseline, candidate = specification.cohorts
    collection_options = {
        "pass_threshold": specification.pass_threshold,
        "budget_exhaustion_is_failure": specification.budget_exhaustion_is_failure,
    }
    left, left_warnings = collect_cohort_trials(
        root, baseline, specification.reward_name, **collection_options
    )
    right, right_warnings = collect_cohort_trials(
        root, candidate, specification.reward_name, **collection_options
    )
    observations = [*left, *right]
    if evidence_kind != "fixture" and any(t.evidence_kind == "fixture" for t in observations):
        raise ValueError(
            "CPU fixture records cannot be relabeled as historical or model-run evidence"
        )
    if evidence_kind == "model-run" and any(
        t.agent_name in {"oracle", "nop"} for t in observations
    ):
        raise ValueError("Oracle/nop records are controls, not model-run comparison evidence")
    pairs = evaluate_pairs(specification, left, right)
    outcomes = {
        kind: sum(p.delta is not None and p.delta.classification == kind for p in pairs)
        for kind in ("positive", "neutral", "negative")
    }
    deltas = [
        p.delta.effective_reward_delta
        for p in pairs
        if p.delta is not None and p.delta.effective_reward_delta is not None
    ]
    warnings = list(dict.fromkeys([*left_warnings, *right_warnings]))
    warnings.append(
        "Descriptive whole-agent configuration contrast; no significance or causal benefit claim."
    )
    warnings.append(
        "Total budgets are not proven matched. Report worker overhead separately; never optimize for shortest trace."
    )
    kinds = {
        "fixture": "synthetic_cpu_fixture",
        "historical": "retrospective_historical_evidence",
        "model-run": "caller_declared_model_run_evidence",
    }
    return {
        "metadata": {
            "schema_version": 1,
            "spec_id": specification.comparison_id,
            "experiment_id": specification.experiment_id,
            "evidence_kind": evidence_kind,
            "specimen_origin": kinds[evidence_kind],
            "evidence_kind_authority": "caller declaration checked against known fixture/control records; not execution authorization",
            "pairing_key": specification.pairing_key,
            "reward_name": specification.reward_name,
            "pass_threshold": specification.pass_threshold,
            "baseline_cohort_label": baseline.label,
            "candidate_cohort_label": candidate.label,
            "analysis_identity": _analysis_identity(),
            "repo_root": str(root),
            "spec_sha256": hashlib.sha256(
                json.dumps(specification.model_dump(mode="json"), sort_keys=True).encode()
            ).hexdigest(),
            "source_inputs_sha256": _input_manifest(root, observations),
            "inference_level": "descriptive_only",
            "compute_budget_match": "not_established",
        },
        "summary": {
            "counts_and_denominators": {
                "n_total_baseline_trials": len(left),
                "n_total_candidate_trials": len(right),
                "n_total_keys_considered": len(pairs),
                "n_unambiguous_pairs": sum(p.pairing_status == "unambiguous_pair" for p in pairs),
                "n_ambiguous_pairs": sum(
                    p.pairing_status == "ambiguous_duplicate_attempts" for p in pairs
                ),
                "n_partial_baseline_only": sum(
                    p.pairing_status == "missing_candidate" for p in pairs
                ),
                "n_partial_candidate_only": sum(
                    p.pairing_status == "missing_baseline" for p in pairs
                ),
                "n_qualified_pairs": sum(p.qualification.is_qualified for p in pairs),
            },
            "outcomes": {
                "n_positive_deltas": outcomes["positive"],
                "n_neutral_deltas": outcomes["neutral"],
                "n_negative_deltas": outcomes["negative"],
                "n_unsupported_pairs": sum(
                    p.delta is None or p.delta.classification == "unsupported" for p in pairs
                ),
                "mean_effective_reward_delta": sum(deltas) / len(deltas) if deltas else None,
            },
            "baseline_cohort_summary": summarize_cohort(
                baseline.label, left, specification.pass_threshold
            ),
            "candidate_cohort_summary": summarize_cohort(
                candidate.label, right, specification.pass_threshold
            ),
            "warnings": warnings,
        },
        "per_task_pairs": [p.to_dict() for p in pairs],
    }


def _publish(report: dict[str, Any], output: Path, *, spec_path: Path, root: Path) -> None:
    specification = _load_spec(spec_path)
    for selector in specification.cohorts:
        for raw in selector.paths:
            source = safe_repo_path(root, raw)
            if output == source or output.is_relative_to(source) or source.is_relative_to(output):
                raise ValueError("Analysis output must not overlap selected source evidence")
    if any(output / name == spec_path for name in ("report.json", "report.md", "plot.svg")):
        raise ValueError("Analysis output would overwrite its input spec")
    contents = {
        "report.json": json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        "report.md": render_markdown_report(report) + "\n",
        "plot.svg": render_svg_plot(report) + "\n",
    }
    if output.exists():
        if output.is_dir() and all(
            (output / name).is_file()
            and not (output / name).is_symlink()
            and (output / name).read_text() == text
            for name, text in contents.items()
        ):
            return
        raise FileExistsError(f"Refusing to overwrite differing analysis output: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".har13-", dir=output.parent))
    try:
        for name, text in contents.items():
            (temporary / name).write_text(text)
        os.rename(temporary, output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path, help="Existing CohortComparisonSpec JSON")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--evidence-kind", choices=sorted(VALID_EVIDENCE_KINDS), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        root = args.repo_root.resolve()
        spec_path = args.spec.resolve(strict=True)
        if args.output.is_symlink():
            raise ValueError("Output directory cannot be a symlink")
        report = analyze(spec_path, repo_root=root, evidence_kind=args.evidence_kind)
        _publish(report, args.output.resolve(), spec_path=spec_path, root=root)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    counts = report["summary"]["counts_and_denominators"]
    print(
        f"{report['metadata']['spec_id']}: {args.evidence_kind}; "
        f"{counts['n_qualified_pairs']}/{counts['n_total_keys_considered']} qualified descriptive pairs"
    )
    print(f"JSON/Markdown/SVG: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
