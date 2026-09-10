"""CLI and reader integration for Harness Mechanics Lab."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sys
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from evallab.trajectory_ir import TrajectoryIR, build_trajectory_ir

MAX_TRAJECTORY_BYTES = 100 * 1024 * 1024  # 100 MiB limit for bounded JSON validation
VALID_EVIDENCE_KINDS = frozenset({"historical", "fixture", "model-run"})
CONTROL_AGENTS = frozenset(
    {"oracle", "nop", "control", "oracle_agent", "nop_agent", "oracle-agent", "nop-agent"}
)


def _extract_identity(
    raw_data: dict[str, Any], ir: TrajectoryIR | None = None
) -> dict[str, str | None]:
    """Extract identity fields (session_id, model, harness) safely without hallucinating."""
    session_id: str | None = None
    if ir and ir.session_id:
        session_id = ir.session_id
    elif isinstance(raw_data.get("session_id"), str) and raw_data["session_id"].strip():
        session_id = raw_data["session_id"].strip()
    elif isinstance(raw_data.get("trajectory_id"), str) and raw_data["trajectory_id"].strip():
        session_id = raw_data["trajectory_id"].strip()

    raw_agent = raw_data.get("agent")
    agent_data: dict[str, Any] = raw_agent if isinstance(raw_agent, dict) else {}

    model: str | None = None
    if isinstance(agent_data.get("model_name"), str) and agent_data["model_name"].strip():
        model = agent_data["model_name"].strip()
    elif isinstance(agent_data.get("model"), str) and agent_data["model"].strip():
        model = agent_data["model"].strip()
    elif isinstance(raw_data.get("model_name"), str) and raw_data["model_name"].strip():
        model = raw_data["model_name"].strip()
    elif isinstance(raw_data.get("model"), str) and raw_data["model"].strip():
        model = raw_data["model"].strip()
    elif ir and ir.model_name and ir.model_name != "unknown":
        model = ir.model_name

    harness: str | None = None
    raw_extra = raw_data.get("extra")
    extra_data: dict[str, Any] = raw_extra if isinstance(raw_extra, dict) else {}
    agent_extra = agent_data.get("extra")
    agent_extra_data: dict[str, Any] = agent_extra if isinstance(agent_extra, dict) else {}

    for candidate in (
        raw_data.get("harness"),
        agent_data.get("harness"),
        extra_data.get("harness"),
        agent_extra_data.get("harness"),
        agent_extra_data.get("originator"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            harness = candidate.strip()
            break

    if harness is None and isinstance(agent_data.get("name"), str) and agent_data["name"].strip():
        name = agent_data["name"].strip()
        if name != "unknown":
            harness = name

    return {
        "session_id": session_id,
        "model": model,
        "harness": harness,
    }


def _is_control(raw_data: dict[str, Any]) -> bool:
    """Detect control runs (oracle/nop) that must not be treated as agent behavior.

    Only exact matches against control identifiers and explicit control flags are rejected.
    Ordinary named agents (e.g. 'canopy', 'sinopia') are never rejected by substring matching.
    """
    if raw_data.get("is_control") is True or raw_data.get("control") is True:
        return True
    raw_extra = raw_data.get("extra")
    if isinstance(raw_extra, dict) and (
        raw_extra.get("is_control") is True or raw_extra.get("control") is True
    ):
        return True
    raw_agent = raw_data.get("agent")
    if isinstance(raw_agent, dict):
        if raw_agent.get("is_control") is True or raw_agent.get("control") is True:
            return True
        agent_name = str(raw_agent.get("name") or "").lower().strip()
        if agent_name in CONTROL_AGENTS:
            return True
    top_agent = str(raw_data.get("agent_name") or "").lower().strip()
    return top_agent in CONTROL_AGENTS


def _looks_like_trajectory(raw_data: dict[str, Any]) -> bool:
    """Check if top-level dictionary looks like an ATIF or Harbor trajectory.

    Requires ATIF schema version, steps field, or trajectory_id with agent to avoid
    permissively admitting arbitrary JSON configurations as agent traces.
    """
    schema_version = raw_data.get("schema_version")
    if isinstance(schema_version, str) and "atif" in schema_version.lower():
        return True
    if "steps" in raw_data:
        return True
    return bool("trajectory_id" in raw_data and isinstance(raw_data.get("agent"), dict))


def _validate_trajectory_shapes(raw_data: dict[str, Any]) -> str | None:
    """Validate nested steps, tool calls, and observation shapes before permissive IR conversion.

    Rejects malformed structures as errors to preserve zero-based source index integrity.
    Returns an error message string if malformed, or None if valid.
    """
    if "steps" not in raw_data:
        return None

    raw_steps = raw_data["steps"]
    if not isinstance(raw_steps, list):
        return f"Field 'steps' must be a list, got {type(raw_steps).__name__}"

    for step_idx, step in enumerate(raw_steps):
        if not isinstance(step, dict):
            return f"Step at index {step_idx} must be an object, got {type(step).__name__}"
        if not step:
            return f"Step at index {step_idx} must not be an empty object"
        if "tool_calls" in step:
            tc_list = step["tool_calls"]
            if not isinstance(tc_list, list):
                return f"Field 'tool_calls' in step {step_idx} must be a list, got {type(tc_list).__name__}"
            for tc_idx, tc in enumerate(tc_list):
                if not isinstance(tc, dict):
                    return (
                        f"Tool call at index {tc_idx} in step {step_idx} must be an object, "
                        f"got {type(tc).__name__}"
                    )

        if "tool_call" in step:
            tc = step["tool_call"]
            if not isinstance(tc, dict):
                return f"Field 'tool_call' in step {step_idx} must be an object, got {type(tc).__name__}"

        if "observation_results" in step:
            obs_list = step["observation_results"]
            if not isinstance(obs_list, list):
                return (
                    f"Field 'observation_results' in step {step_idx} must be a list, "
                    f"got {type(obs_list).__name__}"
                )
            for obs_idx, obs in enumerate(obs_list):
                if not isinstance(obs, dict):
                    return (
                        f"Observation result at index {obs_idx} in step {step_idx} must be an object, "
                        f"got {type(obs).__name__}"
                    )
            # Flattened observation_results represents normalized IR schema rather than raw native ATIF observation.results.
            # Locators in analysis report must point to original raw ATIF paths (/observation/results/...).
            if "observation" not in step:
                return (
                    f"Step at index {step_idx} uses flattened 'observation_results' without native 'observation.results'; "
                    "analyzer requires raw native ATIF source input to preserve valid RFC6901 pointers"
                )

        if "observation" in step:
            obs_obj = step["observation"]
            if isinstance(obs_obj, dict) and "results" in obs_obj:
                res_list = obs_obj["results"]
                if not isinstance(res_list, list):
                    return (
                        f"Field 'observation.results' in step {step_idx} must be a list, "
                        f"got {type(res_list).__name__}"
                    )
                for res_idx, res in enumerate(res_list):
                    if not isinstance(res, dict):
                        return (
                            f"Observation result at index {res_idx} in step {step_idx} must be an object, "
                            f"got {type(res).__name__}"
                        )

        if "observations" in step:
            obs_list = step["observations"]
            if isinstance(obs_list, list):
                for obs_idx, obs in enumerate(obs_list):
                    if not isinstance(obs, dict):
                        return (
                            f"Observation at index {obs_idx} in step {step_idx} must be an object, "
                            f"got {type(obs).__name__}"
                        )
            elif not isinstance(obs_list, dict):
                return (
                    f"Field 'observations' in step {step_idx} must be a list or object, "
                    f"got {type(obs_list).__name__}"
                )

    return None


def process_trajectory_source(
    raw_path: str,
    resolved_path: Path,
    repo_root: Path,
    diagnostics_runner: Callable[[TrajectoryIR], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Process a single trajectory file and return a structured record according to contract.

    Source files are opened in read-only mode and never modified.
    Raw content/prompts are never echoed into warnings or error messages.
    """
    source_info: dict[str, Any] = {
        "path": raw_path,
        "sha256": None,
        "size_bytes": None,
    }
    record: dict[str, Any] = {
        "source": source_info,
        "identity": {
            "session_id": None,
            "model": None,
            "harness": None,
        },
        "status": "error",
        "diagnostics": {},
        "warnings": [],
    }

    if not os.path.isabs(raw_path):
        try:
            rel_path = resolved_path.relative_to(repo_root).as_posix()
            record["source"]["path"] = rel_path
        except ValueError:
            record["status"] = "error"
            record["warnings"].append(f"Relative source path escapes repository root: {raw_path}")
            return record
    else:
        try:
            rel_path = resolved_path.relative_to(repo_root).as_posix()
        except ValueError:
            rel_path = resolved_path.as_posix()
        record["source"]["path"] = rel_path
    if not resolved_path.exists():
        record["warnings"].append(f"Source file does not exist: {rel_path}")
        return record

    if not resolved_path.is_file():
        record["warnings"].append(f"Source path is not a regular file: {rel_path}")
        return record

    try:
        with open(resolved_path, "rb") as f:
            raw_bytes = f.read()
    except Exception as exc:
        record["warnings"].append(f"Failed to read source file: {exc.__class__.__name__}")
        return record

    record["source"]["size_bytes"] = len(raw_bytes)
    record["source"]["sha256"] = hashlib.sha256(raw_bytes).hexdigest()

    if len(raw_bytes) > MAX_TRAJECTORY_BYTES:
        record["warnings"].append(
            f"Source file exceeds maximum allowed size ({len(raw_bytes)} bytes > {MAX_TRAJECTORY_BYTES})"
        )
        return record

    try:
        text = raw_bytes.decode("utf-8")
        raw_json = json.loads(text)
    except UnicodeDecodeError:
        record["warnings"].append("Source file is not valid UTF-8")
        return record
    except json.JSONDecodeError as err:
        record["warnings"].append(f"Invalid JSON at line {err.lineno} column {err.colno}")
        return record

    if not isinstance(raw_json, dict):
        record["warnings"].append(
            f"Top-level JSON element must be an object, got {type(raw_json).__name__}"
        )
        return record

    # Extract identity safely
    record["identity"] = _extract_identity(raw_json)

    # Check for controls (oracle / nop)
    if _is_control(raw_json):
        record["status"] = "unsupported"
        record["warnings"].append(
            "Control run (oracle/nop) does not represent evaluated agent behavior"
        )
        return record

    # Check if this has trajectory structure
    if not _looks_like_trajectory(raw_json):
        record["status"] = "unsupported"
        record["warnings"].append("Non-trajectory format: missing ATIF schema, agent, or steps")
        return record

    # Validate steps and nested shapes before permissive IR conversion
    shape_error = _validate_trajectory_shapes(raw_json)
    if shape_error is not None:
        record["status"] = "error"
        record["warnings"].append(shape_error)
        return record

    # Build canonical TrajectoryIR with store_root=None
    try:
        ir = build_trajectory_ir(
            raw_data=raw_json,
            store_root=None,
            source_path=rel_path,
            source_sha256=record["source"]["sha256"] or "",
        )
    except Exception as exc:
        record["status"] = "error"
        record["warnings"].append(f"Failed to convert to TrajectoryIR: {exc.__class__.__name__}")
        return record

    # Update identity with any fields resolved by TrajectoryIR
    record["identity"] = _extract_identity(raw_json, ir)

    # Run diagnostics
    try:
        if diagnostics_runner is not None:
            diagnostics = diagnostics_runner(ir)
        else:
            diagnostics = _run_default_diagnostics(ir)
    except Exception as exc:
        record["status"] = "error"
        record["warnings"].append(f"Diagnostic execution failed: {exc.__class__.__name__}")
        return record

    record["status"] = "analyzed"
    record["diagnostics"] = diagnostics
    return record


def _run_default_diagnostics(ir: TrajectoryIR) -> dict[str, Any]:
    """Execute stopping, clipping, and shell diagnostic inspectors."""
    from harness_mechanics.clipping import inspect as inspect_clipping
    from harness_mechanics.shell import inspect as inspect_shell
    from harness_mechanics.stopping import inspect as inspect_stopping

    return {
        "stopping": inspect_stopping(ir),
        "clipping": inspect_clipping(ir),
        "shell": inspect_shell(ir),
    }


def _run_default_report(
    records: list[dict[str, Any]], evidence_kind: str
) -> tuple[dict[str, Any], str]:
    """Build report dictionary and rendered text using report module."""
    from harness_mechanics.report import build_report, render_text

    report = build_report(records, evidence_kind=evidence_kind)
    rendered = render_text(report)
    return report, rendered


def _run_default_ablation(report: dict[str, Any]) -> dict[str, Any]:
    """Build ablation plan dictionary using ablation module."""
    from harness_mechanics.ablation import build_ablation_plan

    return build_ablation_plan(report)


def parse_args(args: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for harness-mechanics analyzer."""
    parser = argparse.ArgumentParser(
        prog="analyze.py",
        description="Source-grounded observational diagnostics over retained Harbor trajectories.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--trajectory",
        action="append",
        type=str,
        dest="trajectories",
        help="Path to trajectory file (repeatable).",
    )
    group.add_argument(
        "--manifest",
        type=str,
        help="Path to manifest JSON file.",
    )
    parser.add_argument(
        "--repo-root",
        type=str,
        required=True,
        help="Path to repository root directory.",
    )
    parser.add_argument(
        "--output",
        type=str,
        required=True,
        help="Output directory for report.json, report.txt, and ablation-plan.json.",
    )
    parser.add_argument(
        "--evidence-kind",
        choices=["historical", "fixture", "model-run"],
        default=None,
        help="User-declared provenance of the evaluated trajectories.",
    )
    return parser.parse_args(args)


def write_outputs_atomically(
    output_dir: Path,
    report: dict[str, Any],
    report_text: str,
    ablation_plan: dict[str, Any],
) -> None:
    """Write report.json, report.txt, and ablation-plan.json atomically in a new output directory.

    Refuses if output_dir already exists or is a symlink (including dangling symlinks).
    """
    if output_dir.is_symlink() or output_dir.exists():
        raise FileExistsError(f"Refusing to overwrite existing output directory: {output_dir.name}")

    report_json_content = json.dumps(report, indent=2) + "\n"
    ablation_plan_content = json.dumps(ablation_plan, indent=2) + "\n"

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    # Stage to a unique sibling directory on the same filesystem
    stage_dir = output_dir.parent / f".tmp_{output_dir.name}_{uuid.uuid4().hex}"
    stage_dir.mkdir(parents=True, exist_ok=False)
    try:
        (stage_dir / "report.json").write_text(report_json_content, encoding="utf-8")
        (stage_dir / "report.txt").write_text(report_text, encoding="utf-8")
        (stage_dir / "ablation-plan.json").write_text(ablation_plan_content, encoding="utf-8")
        os.replace(stage_dir, output_dir)
    except Exception:
        # Clean up staging directory on error
        if stage_dir.is_symlink() or stage_dir.exists():
            for item in stage_dir.iterdir():
                with contextlib.suppress(OSError):
                    item.unlink()
            with contextlib.suppress(OSError):
                stage_dir.rmdir()
        raise


def run(
    args: argparse.Namespace,
    diagnostics_runner: Callable[[TrajectoryIR], dict[str, Any]] | None = None,
    report_runner: Callable[[list[dict[str, Any]], str], tuple[dict[str, Any], str]] | None = None,
    ablation_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> int:
    """Execute analysis pipeline according to CLI contract.

    Returns:
        0 on all supported valid records.
        2 on invalid inputs, unsupported records, or error records.
    """
    repo_root = Path(args.repo_root).resolve()
    if not repo_root.exists() or not repo_root.is_dir():
        sys.stderr.write(
            f"Error: repo-root does not exist or is not a directory: {args.repo_root}\n"
        )
        return 2

    output_dir = Path(args.output)
    if output_dir.is_symlink() or output_dir.exists():
        sys.stderr.write(
            f"Error: Output directory already exists or is a symlink: {output_dir.name}\n"
        )
        return 2
    resolved_output_dir = output_dir.resolve()
    if resolved_output_dir.is_symlink() or resolved_output_dir.exists():
        sys.stderr.write(
            f"Error: Output directory already exists or is a symlink: {output_dir.name}\n"
        )
        return 2

    raw_trajectories: list[str] = []
    effective_evidence_kind: str = "historical"

    if args.manifest:
        is_manifest_abs = os.path.isabs(args.manifest)
        manifest_p = Path(args.manifest) if is_manifest_abs else (repo_root / args.manifest)
        if not is_manifest_abs:
            try:
                manifest_path = manifest_p.resolve()
                manifest_path.relative_to(repo_root)
            except ValueError:
                sys.stderr.write(
                    f"Error: Relative manifest path escapes repository root: {args.manifest}\n"
                )
                return 2
        else:
            manifest_path = manifest_p.resolve()

        if not manifest_path.is_file():
            sys.stderr.write(f"Error: Manifest file not found: {manifest_path.name}\n")
            return 2
        try:
            manifest_bytes = manifest_path.read_bytes()
            manifest_data = json.loads(manifest_bytes.decode("utf-8"))
        except Exception as exc:
            sys.stderr.write(f"Error: Failed to read manifest JSON: {exc.__class__.__name__}\n")
            return 2

        if not isinstance(manifest_data, dict):
            sys.stderr.write("Error: Manifest JSON must be an object\n")
            return 2

        manifest_kind = manifest_data.get("evidence_kind")
        if not isinstance(manifest_kind, str) or manifest_kind not in VALID_EVIDENCE_KINDS:
            sys.stderr.write(
                f"Error: Manifest evidence_kind must be one of {sorted(VALID_EVIDENCE_KINDS)}, got {manifest_kind!r}\n"
            )
            return 2

        if args.evidence_kind is not None and args.evidence_kind != manifest_kind:
            sys.stderr.write(
                f"Error: Conflicting evidence_kind: CLI specified {args.evidence_kind!r} but manifest specified {manifest_kind!r}\n"
            )
            return 2

        effective_evidence_kind = args.evidence_kind or manifest_kind

        trajs = manifest_data.get("trajectories")
        if not isinstance(trajs, list):
            sys.stderr.write("Error: Manifest 'trajectories' must be a list of paths\n")
            return 2
        for t in trajs:
            if not isinstance(t, str):
                sys.stderr.write(
                    f"Error: Manifest trajectory path must be a string, got {type(t).__name__}\n"
                )
                return 2
            raw_trajectories.append(t)
    else:
        effective_evidence_kind = args.evidence_kind or "historical"
        raw_trajectories.extend(args.trajectories or [])

    if not raw_trajectories:
        sys.stderr.write("Error: No trajectory paths provided\n")
        return 2

    # Deduplicate by resolved path while preserving order
    seen_resolved: set[Path] = set()
    unique_targets: list[tuple[str, Path]] = []

    for raw_p in raw_trajectories:
        is_abs = os.path.isabs(raw_p)
        p_obj = Path(raw_p) if is_abs else (repo_root / raw_p)
        if not is_abs:
            try:
                resolved_p = p_obj.resolve()
                resolved_p.relative_to(repo_root)
            except ValueError:
                sys.stderr.write(
                    f"Error: Relative trajectory path escapes repository root: {raw_p}\n"
                )
                return 2
        else:
            resolved_p = p_obj.resolve()

        if resolved_p not in seen_resolved:
            seen_resolved.add(resolved_p)
            unique_targets.append((raw_p, resolved_p))

    # Process records
    records: list[dict[str, Any]] = []
    has_unsupported_or_error = False

    for raw_p, resolved_p in unique_targets:
        record = process_trajectory_source(
            raw_path=raw_p,
            resolved_path=resolved_p,
            repo_root=repo_root,
            diagnostics_runner=diagnostics_runner,
        )
        records.append(record)
        if record["status"] in ("unsupported", "error"):
            has_unsupported_or_error = True

    # Build report and ablation proposals
    try:
        if report_runner is not None:
            report, report_text = report_runner(records, effective_evidence_kind)
        else:
            report, report_text = _run_default_report(records, effective_evidence_kind)
    except ValueError as exc:
        sys.stderr.write(f"Error: Validation failed building report: {exc.__class__.__name__}\n")
        return 2
    except Exception as exc:
        sys.stderr.write(f"Error: Failed to build report: {exc.__class__.__name__}\n")
        return 2

    try:
        if ablation_runner is not None:
            ablation_plan = ablation_runner(report)
        else:
            ablation_plan = _run_default_ablation(report)
    except ValueError as exc:
        sys.stderr.write(
            f"Error: Validation failed building ablation plan: {exc.__class__.__name__}\n"
        )
        return 2
    except Exception as exc:
        sys.stderr.write(f"Error: Failed to build ablation plan: {exc.__class__.__name__}\n")
        return 2

    # Publish outputs atomically
    try:
        write_outputs_atomically(
            output_dir=output_dir,
            report=report,
            report_text=report_text,
            ablation_plan=ablation_plan,
        )
    except Exception as exc:
        sys.stderr.write(f"Error: Failed to write outputs: {exc.__class__.__name__}\n")
        return 2

    if has_unsupported_or_error:
        return 2

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Main CLI entrypoint."""
    try:
        args = parse_args(argv)
    except SystemExit as err:
        return 2 if err.code != 0 else 0
    except ValueError as err:
        sys.stderr.write(f"Error: Invalid arguments: {err.__class__.__name__}\n")
        return 2
    return run(args)
