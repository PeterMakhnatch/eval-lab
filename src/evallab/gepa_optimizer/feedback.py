"""Feedback builder for GEPA prompt optimizer and reflection.

Builds bounded, real, agent-visible feedback from task instructions,
trial trajectories (via normalized TrajectoryIR steps), logs, and emitted
verifier diagnostics. Enforces path jails, central redaction semantics,
and character limits without unbounded duplicates.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from evallab.explorer import redact_text
from evallab.registry import task_directory_digest
from evallab.trajectory_ir import build_trajectory_ir

__all__ = [
    "build_feedback",
    "build_prior_run_feedback",
    "validate_oracle_reference",
    "validate_prior_run_reference",
]

_FORBIDDEN_TASK_PARTS = frozenset({"tests", "solution"})
_MAX_OBSERVATION_CHARS = 1000
_ERROR_PATTERNS = (
    "Traceback",
    "command not found",
    "No such file",
    "Permission denied",
    "SyntaxError",
    "NameError",
    "TypeError",
    "ValueError",
    "AttributeError",
    "ImportError",
    "ModuleNotFoundError",
    "FileNotFoundError",
    "KeyError",
    "IndexError",
    "AssertionError",
    "FATAL:",
    "ERROR:",
    "FAILED",
)


def _collect_secrets() -> frozenset[str]:
    """Collect known host secret values from existing Lab credential handlers once."""
    secrets: set[str] = set()
    try:
        from evallab.execution_contracts import collected_secret_values

        secrets.update(s for s in collected_secret_values() if s)
    except ImportError:
        pass
    try:
        from evallab.harbor_zai_opencode import collected_zai_secret_values

        secrets.update(s for s in collected_zai_secret_values() if s)
    except ImportError:
        pass
    return frozenset(secrets)


def _redact_full_text(text: str, secrets: frozenset[str]) -> str:
    """Redact secret tokens, passwords, bearer headers, and known API keys."""
    if not text:
        return ""
    redacted = redact_text(text)
    for s in secrets:
        if len(s) >= 4 and s in redacted:
            redacted = redacted.replace(s, "[redacted]")
    return redacted


def _extract_command(args: Any) -> str | None:
    """Extract a representative command string from tool call arguments."""
    if isinstance(args, str):
        return args.strip()
    if isinstance(args, dict):
        for key in ("cmd", "command", "input", "script", "code"):
            val = args.get(key)
            if isinstance(val, str):
                return val.strip()
        try:
            return json.dumps(args, sort_keys=True)
        except Exception:
            return None
    return None


def _find_error_indicator(obs_text: str) -> str | None:
    if not obs_text:
        return None
    for line in obs_text.splitlines():
        s = line.strip()
        if any(pat in s for pat in _ERROR_PATTERNS):
            return s[:160]
    return None


def _validate_path_jail(root: Path, target: Path | str, *, label: str) -> Path:
    """Resolve target strictly within root; reject path traversal and escaping symlinks."""
    resolved_root = root.resolve()
    target_path = Path(target)
    candidate = target_path if target_path.is_absolute() else (resolved_root / target_path)
    resolved_target = candidate.resolve()
    try:
        resolved_target.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(
            f"{label} '{target}' escapes allowed root '{root}' via traversal or symlink"
        ) from exc
    return resolved_target


def _safe_child_file(parent_dir: Path, rel_path: str, *, label: str) -> Path | None:
    """Resolve child file strictly within parent_dir; reject symlink/path traversal escapes."""
    target = parent_dir / rel_path
    current = target
    while current != parent_dir:
        if current.is_symlink():
            raise ValueError(f"{label} escapes allowed root through an untrusted symlink")
        current = current.parent
    if not target.exists():
        return None
    resolved = target.resolve()
    try:
        resolved.relative_to(parent_dir.resolve())
    except ValueError as exc:
        raise ValueError(
            f"{label} '{rel_path}' under '{parent_dir}' escapes allowed root via symlink or traversal"
        ) from exc
    return resolved


def _check_task_path_safety(task_path: Path) -> None:
    for part in task_path.parts:
        if part.lower() in _FORBIDDEN_TASK_PARTS:
            raise ValueError(
                f"task_path '{task_path}' accesses forbidden hidden verification directory: '{part}'"
            )


def validate_oracle_reference(repo_root: Path, task_path: Path, reference: dict) -> Path:
    """Preflight validate an oracle reference against safety, digest, execution, and metadata rules."""
    if not isinstance(reference, dict):
        raise ValueError("oracle_reference must be a dictionary")
    if set(reference.keys()) != {"trial_path", "result_sha256", "task_package_digest"}:
        raise ValueError(
            "oracle_reference must contain exact keys ['result_sha256', 'task_package_digest', 'trial_path']"
        )
    for k, v in reference.items():
        if not isinstance(v, (str, Path)) or not str(v).strip():
            raise ValueError(f"oracle_reference key '{k}' must be a non-empty string or Path")

    if not repo_root.is_dir():
        raise FileNotFoundError(f"repo_root '{repo_root}' does not exist or is not a directory")
    repo = repo_root.resolve()
    _check_task_path_safety(Path(task_path))
    task = _validate_path_jail(repo, task_path, label="task_path")
    _check_task_path_safety(task)
    if not task.is_dir():
        raise FileNotFoundError(f"task_path '{task_path}' does not exist or is not a directory")

    raw_trial = Path(reference["trial_path"])
    _check_task_path_safety(raw_trial)
    trial = _validate_path_jail(repo, raw_trial, label="oracle_reference trial_path")
    if any(p.lower() in _FORBIDDEN_TASK_PARTS for p in trial.relative_to(repo).parts):
        raise ValueError("oracle_reference trial_path accesses forbidden hidden directory")
    unresolved = raw_trial if raw_trial.is_absolute() else repo / raw_trial
    if any(path.is_symlink() for path in (unresolved, *unresolved.parents)):
        raise ValueError("oracle_reference trial_path must not contain symlinks")
    if not trial.is_dir():
        raise FileNotFoundError(
            f"oracle_reference trial_path '{raw_trial}' does not exist or is not a directory"
        )

    res_file = _safe_child_file(trial, "result.json", label="oracle trial result")
    if res_file is None or not res_file.is_file():
        raise FileNotFoundError(f"oracle result.json not found under '{trial}'")
    res_bytes = res_file.read_bytes()
    actual_sha, exp_sha = (
        hashlib.sha256(res_bytes).hexdigest(),
        str(reference["result_sha256"]).strip(),
    )
    exp_hex = exp_sha[7:] if exp_sha.startswith("sha256:") else exp_sha
    if actual_sha.lower() != exp_hex.lower():
        raise ValueError(
            f"oracle_reference result_sha256 mismatch: expected {exp_sha}, computed {actual_sha}"
        )

    try:
        res_data = json.loads(res_bytes.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"oracle trial result.json is unparseable: {exc}") from exc
    if not isinstance(res_data, dict):
        raise ValueError("oracle trial result.json is not a valid JSON object")
    if not res_data.get("finished_at"):
        raise ValueError("oracle trial did not finish successfully (missing finished_at)")
    if res_data.get("status") and str(res_data["status"]) != "completed":
        raise ValueError(f"oracle trial status is {res_data['status']!r}, expected 'completed'")
    if res_data.get("exception_info") or res_data.get("error"):
        raise ValueError(
            f"oracle trial encountered error: {res_data.get('exception_info') or res_data.get('error')}"
        )

    cfg_agent = (
        (res_data.get("config") or {}).get("agent")
        if isinstance(res_data.get("config"), dict)
        else {}
    )
    agent_info = res_data.get("agent_info") if isinstance(res_data.get("agent_info"), dict) else {}
    name = agent_info.get("name") or (
        cfg_agent.get("name") if isinstance(cfg_agent, dict) else None
    )
    if name != "oracle" or (isinstance(cfg_agent, dict) and cfg_agent.get("import_path")):
        raise ValueError(f"oracle trial agent is {name!r}, expected 'oracle'")
    model = (
        (cfg_agent.get("model_name") if isinstance(cfg_agent, dict) else None)
        or agent_info.get("model_info")
        or res_data.get("model")
    )
    if model is not None:
        raise ValueError(f"oracle trial has model {model!r}, expected control agent with no model")

    v_res = res_data.get("verifier_result")
    reward = (v_res.get("rewards") or {}).get("reward") if isinstance(v_res, dict) else None
    if reward is None:
        raise ValueError("oracle trial verifier_result has no primary reward")
    if type(reward) not in (int, float) or reward != 1.0:
        raise ValueError(f"oracle trial primary reward is not numeric 1.0: {reward!r}")

    exp_pkg, comp_pkg = str(reference["task_package_digest"]).strip(), task_directory_digest(task)
    if comp_pkg != exp_pkg:
        raise ValueError(
            f"oracle_reference task_package_digest mismatch: expected {exp_pkg}, task directory computed {comp_pkg}"
        )

    meta_file = _safe_child_file(trial.parent, "lab-metadata.json", label="oracle lab-metadata")
    if meta_file is None or not meta_file.is_file():
        raise FileNotFoundError(
            f"oracle lab-metadata.json not found under job directory '{trial.parent}'"
        )
    try:
        meta_json = json.loads(meta_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"oracle lab-metadata.json is unparseable: {exc}") from exc
    if not isinstance(meta_json, dict) or not isinstance(meta_json.get("experiment"), dict):
        raise ValueError("oracle lab-metadata.json missing 'experiment' section")
    meta_pkg = meta_json["experiment"].get("package_digest")
    if not meta_pkg or meta_pkg != exp_pkg:
        raise ValueError(
            f"oracle lab-metadata experiment.package_digest {meta_pkg!r} does not match expected {exp_pkg!r}"
        )

    return trial


def _task_path_string_from_json(data: Any) -> str | None:
    """Pull a recorded task path string from a trial result.json or config.json object."""
    if not isinstance(data, dict):
        return None
    candidates: list[Any] = []
    cfg = data.get("config")
    if isinstance(cfg, dict):
        candidates.append(cfg.get("task"))
        candidates.append(cfg.get("task_path"))
    candidates.append(data.get("task"))
    candidates.append(data.get("task_path"))
    tid = data.get("task_id")
    if isinstance(tid, dict):
        candidates.append(tid.get("path"))
    for item in candidates:
        if isinstance(item, dict):
            raw = item.get("path")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
        elif isinstance(item, str) and item.strip():
            return item.strip()
    return None


def _task_path_if_inside_repo(repo: Path, raw: str) -> Path | None:
    """Return the jailed task dir when raw points inside repo_root; None if it does not."""
    try:
        resolved = _validate_path_jail(repo, Path(raw), label="task_path")
    except ValueError:
        return None
    if not resolved.is_dir():
        return None
    _check_task_path_safety(resolved)
    return resolved


def _list_trial_dirs(job_dir: Path) -> list[Path]:
    """Index Harbor trial subdirectories under a job dir (same shape as results.load_job)."""
    trials: list[Path] = []
    try:
        children = sorted(job_dir.iterdir())
    except OSError:
        return []
    for candidate in children:
        if not candidate.is_dir() or not (candidate / "result.json").is_file():
            continue
        try:
            loaded = json.loads((candidate / "result.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
        if isinstance(loaded, dict) and "task_name" in loaded and "trial_name" in loaded:
            trials.append(candidate)
    return trials


def _resolve_single_trial_dir(resolved: Path) -> Path:
    """Treat resolved as a trial dir, or a job dir with exactly one trial."""
    result_file = _safe_child_file(resolved, "result.json", label="prior-run result")
    data: dict[str, Any] = {}
    if result_file is not None and result_file.is_file():
        try:
            loaded = json.loads(result_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"result.json under '{resolved}' is unparseable: {exc}") from exc
        if isinstance(loaded, dict):
            data = loaded
    is_trial = "task_name" in data and "trial_name" in data
    is_job = "n_total_trials" in data and "stats" in data
    if is_trial and not is_job:
        return resolved
    trials = _list_trial_dirs(resolved)
    if len(trials) == 1:
        return trials[0]
    if len(trials) > 1:
        names = ", ".join(trial.name for trial in trials)
        raise ValueError(
            f"job directory '{resolved}' contains multiple trials ({names}); "
            "pass a single trial directory"
        )
    raise ValueError(f"no trial found under '{resolved}'")


def _resolve_prior_task_path(repo: Path, trial: Path, job_dir: Path) -> Path:
    """Resolve task_path from the trial's own records, else the job lab-metadata."""
    for label, rel in (("trial result", "result.json"), ("trial config", "config.json")):
        file = _safe_child_file(trial, rel, label=label)
        if file is None or not file.is_file():
            continue
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        raw = _task_path_string_from_json(data)
        if not raw:
            continue
        _check_task_path_safety(Path(raw))
        inside = _task_path_if_inside_repo(repo, raw)
        if inside is not None:
            return inside
    meta = _safe_child_file(job_dir, "lab-metadata.json", label="prior-run lab-metadata")
    if meta is not None and meta.is_file():
        try:
            meta_json = json.loads(meta.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"lab-metadata.json is unparseable: {exc}") from exc
        experiment = meta_json.get("experiment") if isinstance(meta_json, dict) else None
        raw = experiment.get("task_path") if isinstance(experiment, dict) else None
        if isinstance(raw, str) and raw.strip():
            recorded = raw.strip()
            _check_task_path_safety(Path(recorded))
            resolved = _validate_path_jail(repo, recorded, label="lab-metadata task_path")
            _check_task_path_safety(resolved)
            if not resolved.is_dir():
                raise FileNotFoundError(
                    f"lab-metadata experiment.task_path '{recorded}' does not exist or is not a directory"
                )
            return resolved
    raise ValueError(
        f"could not resolve task_path inside repo for prior run '{trial}': "
        "trial result/config task path is absent or outside repo_root and "
        "lab-metadata experiment.task_path is missing"
    )


def validate_prior_run_reference(repo_root: Path, task_path: Path, reference: dict) -> Path:
    """Preflight validate a prior-run reference: exact keys, jail, digest, result hash."""
    if not isinstance(reference, dict):
        raise ValueError("prior_run_reference must be a dictionary")
    if set(reference.keys()) != {"trial_path", "result_sha256", "task_package_digest"}:
        raise ValueError(
            "prior_run_reference must contain exact keys "
            "['result_sha256', 'task_package_digest', 'trial_path']"
        )
    for k, v in reference.items():
        if not isinstance(v, (str, Path)) or not str(v).strip():
            raise ValueError(f"prior_run_reference key '{k}' must be a non-empty string or Path")

    if not repo_root.is_dir():
        raise FileNotFoundError(f"repo_root '{repo_root}' does not exist or is not a directory")
    repo = repo_root.resolve()
    _check_task_path_safety(Path(task_path))
    task = _validate_path_jail(repo, task_path, label="task_path")
    _check_task_path_safety(task)
    if not task.is_dir():
        raise FileNotFoundError(f"task_path '{task_path}' does not exist or is not a directory")

    raw_trial = Path(reference["trial_path"])
    _check_task_path_safety(raw_trial)
    trial = _validate_path_jail(repo, raw_trial, label="prior_run_reference trial_path")
    if any(p.lower() in _FORBIDDEN_TASK_PARTS for p in trial.relative_to(repo).parts):
        raise ValueError("prior_run_reference trial_path accesses forbidden hidden directory")
    unresolved = raw_trial if raw_trial.is_absolute() else repo / raw_trial
    if any(path.is_symlink() for path in (unresolved, *unresolved.parents)):
        raise ValueError("prior_run_reference trial_path must not contain symlinks")
    if not trial.is_dir():
        raise FileNotFoundError(
            f"prior_run_reference trial_path '{raw_trial}' does not exist or is not a directory"
        )

    res_file = _safe_child_file(trial, "result.json", label="prior trial result")
    if res_file is None or not res_file.is_file():
        raise FileNotFoundError(f"prior-run result.json not found under '{trial}'")
    res_bytes = res_file.read_bytes()
    actual_sha = hashlib.sha256(res_bytes).hexdigest()
    exp_sha = str(reference["result_sha256"]).strip()
    exp_hex = exp_sha[7:] if exp_sha.startswith("sha256:") else exp_sha
    if actual_sha.lower() != exp_hex.lower():
        raise ValueError(
            f"prior_run_reference result_sha256 mismatch: expected {exp_sha}, computed {actual_sha}"
        )

    exp_pkg = str(reference["task_package_digest"]).strip()
    comp_pkg = task_directory_digest(task)
    if comp_pkg != exp_pkg:
        raise ValueError(
            f"prior_run_reference task_package_digest mismatch: expected {exp_pkg}, "
            f"task directory computed {comp_pkg}"
        )
    return trial


def _compact_prior_block(
    trial_name: str, feedback_text: str, result_data: dict[str, Any]
) -> list[str]:
    """Lift Trace Status, up to 8 Action+observation lines, and result.json reward/status."""
    lines = [f"### {trial_name}"]
    capturing_obs = False
    action_count = 0
    for line in feedback_text.splitlines():
        if line.startswith("Trace Status:"):
            lines.append(line)
            capturing_obs = False
            continue
        if line.startswith("- Step ") and "Action [" in line:
            if action_count >= 8:
                capturing_obs = False
                continue
            lines.append(line)
            action_count += 1
            capturing_obs = True
            continue
        if capturing_obs and line.startswith("  Observation:"):
            lines.append(line)
            continue
        capturing_obs = False

    if result_data.get("exception_info") or result_data.get("error"):
        status = "error"
    elif "status" in result_data and result_data["status"] is not None:
        status = str(result_data["status"])
    elif result_data:
        status = "completed"
    else:
        status = "unknown"
    verifier_result = result_data.get("verifier_result")
    rewards = verifier_result.get("rewards") if isinstance(verifier_result, dict) else None
    reward = rewards.get("reward") if isinstance(rewards, dict) else None
    if isinstance(reward, float):
        reward_line = f"- Primary Reward: {reward:.4f}"
    elif isinstance(reward, int):
        reward_line = f"- Primary Reward: {reward}"
    else:
        reward_line = "- Primary Reward: n/a"
    lines.append(f"- Status: {status}")
    lines.append(reward_line)
    return lines


def _render_prior_runs(
    *,
    repo: Path,
    task_path: Path,
    prior_trial_paths: Sequence[Path],
    max_chars: int,
    secrets: frozenset[str],
) -> tuple[str, bool, list[str], dict[str, str]]:
    """Build the bounded '## Prior Runs' section by reusing build_feedback on each trial."""
    notices: list[str] = []
    extra_sources: dict[str, str] = {}
    blocks: list[str] = []
    for raw in prior_trial_paths:
        _check_task_path_safety(Path(raw))
        trial = _validate_path_jail(repo, raw, label="prior_trial_path")
        if any(p.lower() in _FORBIDDEN_TASK_PARTS for p in trial.relative_to(repo).parts):
            raise ValueError("prior_trial_path accesses forbidden hidden directory")
        if not trial.is_dir():
            raise FileNotFoundError(
                f"prior_trial_path '{raw}' does not exist or is not a directory"
            )
        inner = build_feedback(
            repo_root=repo,
            task_path=task_path,
            trial_path=trial,
            max_chars=max_chars,
        )
        result_data: dict[str, Any] = {}
        result_file = _safe_child_file(trial, "result.json", label="prior trial result")
        if result_file is not None and result_file.is_file():
            extra_sources[f"prior_run/{trial.name}/trial_result"] = result_file.relative_to(
                repo
            ).as_posix()
            try:
                loaded = json.loads(result_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    result_data = loaded
            except json.JSONDecodeError:
                notices.append(f"prior run '{trial.name}': result.json unparseable")
        block_lines = _compact_prior_block(trial.name, inner["feedback"], result_data)
        blocks.append("\n".join(block_lines))
        for key, value in inner["sources"].items():
            extra_sources[f"prior_run/{trial.name}/{key}"] = value
        if inner.get("truncated"):
            notices.append(f"prior run '{trial.name}' inner feedback was truncated")
    text = _redact_full_text("\n".join(["## Prior Runs", *blocks]), secrets)
    allowance = max_chars // 4
    truncated = len(text) > allowance
    if truncated:
        notices.append("Prior runs truncated to their allocated feedback budget")
        text = text[:allowance]
    return text, truncated, notices, extra_sources


def _extract_state_transitions(
    trial: Path, repo: Path, sources: dict[str, str], prefix: str
) -> tuple[list[str], set[str]]:
    """Extract observable filesystem transitions from state-diff.json or state-events.jsonl."""
    diff = _safe_child_file(trial, "state-journal/state-diff.json", label=f"{prefix} state diff")
    if diff and diff.is_file():
        sources[f"{prefix}_state_diff"] = diff.relative_to(repo).as_posix()
        try:
            data = json.loads(diff.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict) or data.get("status") != "available":
            return [], set()
        trans, paths = [], set()
        for ch in (data.get("changes") or []) if isinstance(data, dict) else []:
            p = ch.get("path") if isinstance(ch, dict) else None
            if p and not any(part.lower() in _FORBIDDEN_TASK_PARTS for part in Path(p).parts):
                paths.add(p)
                node = ch.get("after") or ch.get("before") or {}
                size = f", {node['size_bytes']} bytes" if "size_bytes" in node else ""
                trans.append(
                    f"- [{ch.get('change_type', 'modified')}] {p} ({node.get('type', 'file')}{size})"
                )
        return trans, paths

    events = _safe_child_file(
        trial, "state-journal/state-events.jsonl", label=f"{prefix} state events"
    )
    if events and events.is_file():
        sources[f"{prefix}_state_events"] = events.relative_to(repo).as_posix()
        status_path = _safe_child_file(
            trial, "state-journal/status.json", label=f"{prefix} observer status"
        )
        if status_path is None:
            return [], set()
        try:
            status = json.loads(status_path.read_text())
        except (OSError, json.JSONDecodeError):
            return ["Observer status is unreadable; no file transitions inferred."], set()
        if not isinstance(status, dict) or status.get("status") != "available":
            return [], set()
        trans, paths = [], set()
        for line in events.read_text(encoding="utf-8").splitlines():
            try:
                ev = json.loads(line) if line.strip() else {}
            except json.JSONDecodeError:
                continue
            p = ev.get("path") if isinstance(ev, dict) else None
            if p and not any(part.lower() in _FORBIDDEN_TASK_PARTS for part in Path(p).parts):
                paths.add(p)
                trans.append(f"- [{','.join(ev.get('operations', ['modified']))}] {p}")
        return trans, paths
    return [], set()


def _extract_oracle_actions(
    trial: Path, repo: Path, sources: dict[str, str]
) -> tuple[list[str], list[str], bool, bool]:
    """Extract observable oracle actions without answer payloads, plus raw log command status."""
    actions, log_cmds = [], []
    traj = _safe_child_file(
        trial, "agent/trajectory.json", label="oracle trajectory"
    ) or _safe_child_file(trial, "trajectory.json", label="oracle trajectory")
    if traj and traj.is_file():
        sources["oracle_trajectory"] = traj.relative_to(repo).as_posix()
        try:
            raw = json.loads(traj.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raw = {}
        if isinstance(raw, dict) and raw.get("steps"):
            ir = build_trajectory_ir(raw, source_path=sources["oracle_trajectory"])
            for step in ir.steps:
                if step.source == "agent" and step.tool_calls:
                    for tc in step.tool_calls:
                        actions.append(
                            f"- Step {step.step_id}: observed tool invocation [{tc.function_name}]; "
                            "arguments and output withheld from Oracle reference"
                        )

    log_file = _safe_child_file(trial, "agent/oracle.txt", label="oracle log") or _safe_child_file(
        trial, "oracle.txt", label="oracle log"
    )
    log_present, log_empty = False, True
    if log_file and log_file.is_file():
        sources["oracle_log"] = log_file.relative_to(repo).as_posix()
        log_present = True
        content = log_file.read_text(encoding="utf-8").strip()
        log_empty = not content
    return actions, log_cmds, log_present, log_empty


def build_feedback(
    *,
    repo_root: Path,
    task_path: Path,
    trial_path: Path,
    max_chars: int = 24000,
    oracle_reference: dict[str, Any] | None = None,
    prior_trial_paths: Sequence[Path] = (),
) -> dict[str, Any]:
    """Build bounded, real agent-visible feedback from task and trial artifacts.

    Reads only declared task instruction.md and jailed allowlisted trial artifacts.
    Reuses evallab.trajectory_ir.build_trajectory_ir for normalized steps.
    Redacts full strings before truncating to guarantee a strictly bounded return.

    Args:
        repo_root: Base repository root.
        task_path: Path to the task directory (under repo_root).
        trial_path: Path to the trial directory (under repo_root).
        max_chars: Upper character bound on returned textual feedback.
        oracle_reference: Optional dict with exact keys trial_path, result_sha256,
            and task_package_digest pointing to a completed, successful Oracle run.
        prior_trial_paths: Optional historical trial directories. When supplied and
            oracle_reference is absent, a bounded '## Prior Runs' section is rendered
            before '## Task Instruction'. Absent or empty leaves existing output unchanged.

    Returns:
        Dictionary with single bounded 'feedback' string, sources, coverage_notices,
        truncated flag, char_count, and max_chars.

    Raises:
        ValueError: If paths escape the repo root, target forbidden directories, or symlink out.
        FileNotFoundError: If task_path or trial_path directory does not exist.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")

    if not repo_root.exists() or not repo_root.is_dir():
        raise FileNotFoundError(f"repo_root '{repo_root}' does not exist or is not a directory")

    resolved_repo_root = repo_root.resolve()

    _check_task_path_safety(Path(task_path))

    resolved_task_path = _validate_path_jail(resolved_repo_root, task_path, label="task_path")
    _check_task_path_safety(resolved_task_path)
    if not resolved_task_path.exists() or not resolved_task_path.is_dir():
        raise FileNotFoundError(f"task_path '{task_path}' does not exist or is not a directory")

    resolved_trial_path = _validate_path_jail(resolved_repo_root, trial_path, label="trial_path")
    if not resolved_trial_path.exists() or not resolved_trial_path.is_dir():
        raise FileNotFoundError(f"trial_path '{trial_path}' does not exist or is not a directory")

    sources: dict[str, str] = {}
    coverage_notices: list[str] = []
    secrets = _collect_secrets()

    oracle_data: dict[str, Any] | None = None
    if oracle_reference is not None:
        resolved_oracle = validate_oracle_reference(
            resolved_repo_root, resolved_task_path, oracle_reference
        )
        sources["oracle_trial_result"] = (
            (resolved_oracle / "result.json").relative_to(resolved_repo_root).as_posix()
        )
        meta = _safe_child_file(
            resolved_oracle.parent, "lab-metadata.json", label="oracle lab-metadata"
        )
        if meta and meta.is_file():
            sources["oracle_lab_metadata"] = meta.relative_to(resolved_repo_root).as_posix()
        o_acts, o_cmds, o_pres, o_empty = _extract_oracle_actions(
            resolved_oracle, resolved_repo_root, sources
        )
        if not o_acts:
            coverage_notices.append(
                "oracle_trace: no structured command steps captured; stdout is not an action trace"
            )
        o_trans, o_paths = _extract_state_transitions(
            resolved_oracle, resolved_repo_root, sources, "oracle"
        )
        if not o_trans:
            coverage_notices.append("oracle_state_journal: no usable state transitions captured")
        oracle_data = {
            "task_package_digest": str(oracle_reference["task_package_digest"]).strip(),
            "result_sha256": str(oracle_reference["result_sha256"]).strip(),
            "actions": o_acts,
            "log_cmds": o_cmds,
            "log_present": o_pres,
            "log_empty": o_empty,
            "transitions": o_trans,
            "transition_paths": o_paths,
        }

    # 1. Declared Task Instruction (instruction.md only)
    task_instruction: str | None = None
    inst_file = _safe_child_file(resolved_task_path, "instruction.md", label="task instruction")
    if inst_file is not None and inst_file.is_file():
        task_instruction = inst_file.read_text(encoding="utf-8")
        sources["task_instruction"] = inst_file.relative_to(resolved_repo_root).as_posix()
    else:
        coverage_notices.append(
            "task_instruction: absent (instruction.md not found in task directory)"
        )

    # 2. Trial Result & Outcome (jailed result.json)
    result_file = _safe_child_file(resolved_trial_path, "result.json", label="trial result")
    result_data: dict[str, Any] = {}
    if result_file is not None and result_file.is_file():
        sources["trial_result"] = result_file.relative_to(resolved_repo_root).as_posix()
        try:
            loaded_res = json.loads(result_file.read_text(encoding="utf-8"))
            if isinstance(loaded_res, dict):
                result_data = loaded_res
        except json.JSONDecodeError as exc:
            coverage_notices.append(f"trial_result: unparseable_json ({exc})")

    raw_tid = result_data.get("task_name") or result_data.get("task_id")
    if isinstance(raw_tid, dict):
        task_id = str(raw_tid.get("path") or raw_tid.get("name") or resolved_task_path.name)
    elif raw_tid:
        task_id = str(raw_tid)
    else:
        task_id = resolved_task_path.name

    agent_info = result_data.get("agent_info")
    if not isinstance(agent_info, dict):
        agent_info = {}
    config = result_data.get("config")
    cfg_agent = config.get("agent") if isinstance(config, dict) else None
    agent_name = agent_info.get("name") or (
        cfg_agent.get("name") if isinstance(cfg_agent, dict) else None
    )

    verifier_result = result_data.get("verifier_result")
    if not isinstance(verifier_result, dict):
        verifier_result = {}
    rewards = verifier_result.get("rewards")
    if not isinstance(rewards, dict):
        rewards = {}
    primary_reward: float | None = None
    if "reward" in rewards and isinstance(rewards["reward"], (int, float)):
        primary_reward = float(rewards["reward"])

    raw_error = result_data.get("exception_info") or result_data.get("error")
    error_str = str(raw_error) if raw_error else None

    # Status: reported by result; no status-inferred success
    if error_str:
        outcome_status = "error"
    elif "status" in result_data:
        outcome_status = str(result_data["status"])
    elif result_data:
        outcome_status = "completed"
    else:
        outcome_status = "unknown"

    agent_result = result_data.get("agent_result")
    if not isinstance(agent_result, dict):
        agent_result = {}
    exit_code = agent_result.get("exit_code", result_data.get("exit_code"))

    # Emitted verifier diagnostics (checks.json, test-stdout.txt)
    verifier_diag: str | None = None
    checks_file = _safe_child_file(
        resolved_trial_path, "verifier/checks.json", label="verifier checks"
    )
    if checks_file is not None and checks_file.is_file():
        sources["verifier_checks"] = checks_file.relative_to(resolved_repo_root).as_posix()
        try:
            checks_data = json.loads(checks_file.read_text(encoding="utf-8"))
            if isinstance(checks_data, dict):
                verifier_diag = json.dumps(checks_data, sort_keys=True)
        except json.JSONDecodeError:
            pass

    if verifier_diag is None:
        stdout_file = _safe_child_file(
            resolved_trial_path, "verifier/test-stdout.txt", label="verifier stdout"
        )
        if stdout_file is not None and stdout_file.is_file():
            sources["verifier_stdout"] = stdout_file.relative_to(resolved_repo_root).as_posix()
            raw_stdout = stdout_file.read_text(encoding="utf-8").strip()
            if raw_stdout:
                verifier_diag = _redact_full_text(raw_stdout, secrets)[:_MAX_OBSERVATION_CHARS]

    # 3. Jailed Allowlisted Trajectory via evallab.trajectory_ir.build_trajectory_ir
    traj_file = _safe_child_file(resolved_trial_path, "agent/trajectory.json", label="trajectory")
    if traj_file is None:
        traj_file = _safe_child_file(resolved_trial_path, "trajectory.json", label="trajectory")

    trace_status: str
    actions_summary: list[str] = []
    agent_trace_errors: list[str] = []
    agent_redundant_actions: list[str] = []
    seen_actions: dict[tuple[str, str], list[int]] = {}
    final_response: str | None = None

    if traj_file is None or not traj_file.is_file():
        trace_status = "absent"
        if agent_name in ("nop", "oracle"):
            coverage_notices.append(
                f"trajectory: absent (control agent '{agent_name}' has no model trace)"
            )
        else:
            coverage_notices.append("trajectory: absent (missing trajectory file)")
    else:
        trace_status = "present"
        rel_traj_path = traj_file.relative_to(resolved_repo_root).as_posix()
        sources["trial_trajectory"] = rel_traj_path
        try:
            raw_data = json.loads(traj_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            trace_status = "corrupt"
            coverage_notices.append(f"trajectory: unparseable_json ({exc})")
            raw_data = {}

        if isinstance(raw_data, dict) and raw_data.get("steps"):
            # Map ATIF step.observation.results into observation_results if needed
            for step_dict in raw_data.get("steps", []):
                if (
                    isinstance(step_dict, dict)
                    and "observation_results" not in step_dict
                    and isinstance(step_dict.get("observation"), dict)
                ):
                    res = step_dict["observation"].get("results")
                    if isinstance(res, list):
                        step_dict["observation_results"] = res

            ir = build_trajectory_ir(raw_data, source_path=rel_traj_path)

            for step in ir.steps:
                if step.source == "agent" and step.tool_calls:
                    for tc in step.tool_calls:
                        cmd = _extract_command(tc.arguments) or json.dumps(
                            tc.arguments, sort_keys=True
                        )
                        act_key = (tc.function_name, cmd)
                        if act_key in seen_actions:
                            prior_step = seen_actions[act_key][-1]
                            cmd_disp = cmd[:120] + "..." if len(cmd) > 120 else cmd
                            agent_redundant_actions.append(
                                f"Step {step.step_id} repeated action [{tc.function_name}] from Step {prior_step}: {cmd_disp}"
                            )
                            seen_actions[act_key].append(step.step_id)
                        else:
                            seen_actions[act_key] = [step.step_id]

                        actions_summary.append(
                            f"- Step {step.step_id} Action [{tc.function_name}]: {cmd}"
                        )
                        for obs in step.observation_results:
                            if obs.source_call_id == tc.tool_call_id or not obs.source_call_id:
                                obs_text = (
                                    _redact_full_text(str(obs.content), secrets)
                                    if obs.content is not None
                                    else ""
                                )
                                err_ind = _find_error_indicator(obs_text)
                                if err_ind:
                                    agent_trace_errors.append(
                                        f"Step {step.step_id} Action [{tc.function_name}] observed error: {err_ind}"
                                    )
                                if len(obs_text) > _MAX_OBSERVATION_CHARS:
                                    obs_text = (
                                        obs_text[:_MAX_OBSERVATION_CHARS]
                                        + f"\n... [observation truncated to {_MAX_OBSERVATION_CHARS} chars]"
                                    )
                                actions_summary.append(f"  Observation: {obs_text}")
                if step.reasoning_content:
                    actions_summary.append(f"  Reasoning: {step.reasoning_content}")

            for step in reversed(ir.steps):
                if step.source == "agent":
                    msg = str(step.message).strip() if step.message else ""
                    if msg and not msg.startswith("<<evallab-redacted:"):
                        final_response = msg
                        break

    agent_transition_paths: set[str] = set()
    if oracle_data is not None:
        _, agent_transition_paths = _extract_state_transitions(
            resolved_trial_path, resolved_repo_root, sources, "trial"
        )

    # 4. Format Single Full Text
    lines: list[str] = []
    lines.append(f"# Evaluation Feedback: {task_id}")
    lines.append("")
    oracle_truncated = False
    if oracle_data is not None:
        error_summary = (
            raw_error.get("exception_type") if isinstance(raw_error, dict) else raw_error
        )
        error_summary = _redact_full_text(str(error_summary or "none"), secrets)[:200]
        contrast = [
            "## Oracle Reference Contrast",
            f"Reference result: {oracle_data['result_sha256']}",
            f"Shared package: {oracle_data['task_package_digest']}",
            "Oracle: completed, native reward 1.0, no execution error.",
            f"Agent: {outcome_status}, native reward {primary_reward}, error type {error_summary}.",
            "Oracle captured actions (not a model trajectory or a claim of minimal steps):",
            *(
                oracle_data["actions"]
                or ["No structured command steps captured; raw stdout withheld."]
            ),
            "Oracle observed file transitions:",
            *(oracle_data["transitions"] or ["No usable state transitions captured."]),
            f"Agent tool-error indicators: {agent_trace_errors}",
            f"Repeated agent actions (not necessarily redundant): {agent_redundant_actions}",
            "Transition comparison is descriptive, not a task-output correctness check.",
            f"Oracle-only observed transition paths: {sorted(oracle_data['transition_paths'] - agent_transition_paths)}",
        ]
        text = _redact_full_text("\n".join(contrast), secrets)
        allowance = max_chars // 3
        oracle_truncated = len(text) > allowance
        if oracle_truncated:
            coverage_notices.append("Oracle contrast truncated to its allocated feedback budget")
        lines.extend([text[:allowance], ""])

    prior_truncated = False
    if prior_trial_paths and oracle_data is None:
        prior_text, prior_truncated, prior_notices, prior_sources = _render_prior_runs(
            repo=resolved_repo_root,
            task_path=resolved_task_path,
            prior_trial_paths=prior_trial_paths,
            max_chars=max_chars,
            secrets=secrets,
        )
        coverage_notices.extend(prior_notices)
        sources.update(prior_sources)
        lines.extend([prior_text, ""])

    lines.append("## Task Instruction")
    if task_instruction:
        lines.append(task_instruction.strip())
    else:
        lines.append(
            "Task instruction absent (instruction.md not found in declared task directory)."
        )
    lines.append("")

    lines.append("## Outcome")
    lines.append(f"- Status: {outcome_status}")
    if primary_reward is not None:
        lines.append(
            f"- Primary Reward: {primary_reward:.4f}"
            if isinstance(primary_reward, float)
            else f"- Primary Reward: {primary_reward}"
        )
    if rewards:
        lines.append(f"- Reward Dimensions: {json.dumps(rewards, sort_keys=True)}")
    if exit_code is not None:
        lines.append(f"- Exit Code: {exit_code}")
    if error_str:
        lines.append(f"- Error: {error_str}")
    if verifier_diag:
        lines.append(f"- Verifier Diagnostics: {verifier_diag}")
    lines.append("")

    lines.append("## Execution Trace")
    if trace_status == "absent":
        lines.append(f"Trace Status: absent ({'; '.join(coverage_notices)})")
        lines.append("No agent model trajectory was recorded for this trial.")
    else:
        lines.append(f"Trace Status: {trace_status} (actions={len(actions_summary)})")
        lines.append("")
        for act_line in actions_summary:
            lines.append(act_line)
        if final_response:
            lines.append("")
            lines.append(f"Final Response:\n{final_response}")
    lines.append("")

    lines.append("## Sources & Coverage")
    for src_name, src_path in sorted(sources.items()):
        lines.append(f"- Source [{src_name}]: {src_path}")
    if coverage_notices:
        lines.append("- Notices:")
        for notice in coverage_notices:
            lines.append(f"  * {notice}")

    raw_text = "\n".join(lines).strip() + "\n"

    # 5. Redact Full String Before Truncating
    # All individual excerpts have already been redacted before their local bounds.
    redacted_text = _redact_full_text(raw_text, secrets)

    # 6. Honest Budget Truncation Against max_chars
    truncated = oracle_truncated or prior_truncated
    if len(redacted_text) <= max_chars:
        final_text = redacted_text
    else:
        truncated = True
        coverage_notices.append(f"feedback truncated to max_chars={max_chars} budget")
        trunc_notice = (
            f"\n\n[Truncated: feedback exceeded max_chars budget of {max_chars} characters]"
        )
        if max_chars <= len(trunc_notice):
            final_text = redacted_text[:max_chars]
        else:
            allowed = max_chars - len(trunc_notice)
            final_text = redacted_text[:allowed] + trunc_notice

    return {
        "feedback": final_text,
        "sources": sources,
        "coverage_notices": coverage_notices,
        "truncated": truncated,
        "char_count": len(final_text),
        "max_chars": max_chars,
    }


def build_prior_run_feedback(
    repo_root: Path,
    job_or_trial_dir: Path,
    *,
    max_chars: int = 24000,
) -> dict[str, Any]:
    """Build feedback from an existing Harbor job or trial directory.

    Resolves a single trial (job dirs with exactly one trial are accepted; multiple
    trials raise). Task path comes from the trial's result.json/config.json when that
    path points inside repo_root, otherwise from the job's lab-metadata.json
    experiment.task_path. Hidden tests/solution traversal is rejected. Never reads
    verifier hidden inputs.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")
    if not repo_root.exists() or not repo_root.is_dir():
        raise FileNotFoundError(f"repo_root '{repo_root}' does not exist or is not a directory")
    repo = repo_root.resolve()
    _check_task_path_safety(Path(job_or_trial_dir))
    given = _validate_path_jail(repo, job_or_trial_dir, label="job_or_trial_dir")
    _check_task_path_safety(given)
    unresolved = Path(job_or_trial_dir)
    if not unresolved.is_absolute():
        unresolved = repo / unresolved
    if any(path.is_symlink() for path in (unresolved, *unresolved.parents)):
        raise ValueError("job_or_trial_dir must not contain symlinks")
    if not given.is_dir():
        raise FileNotFoundError(
            f"job_or_trial_dir '{job_or_trial_dir}' does not exist or is not a directory"
        )
    trial = _resolve_single_trial_dir(given)
    _check_task_path_safety(trial)
    if any(p.lower() in _FORBIDDEN_TASK_PARTS for p in trial.relative_to(repo).parts):
        raise ValueError("prior-run trial path accesses forbidden hidden directory")
    job_dir = given if trial != given else given.parent
    task_path = _resolve_prior_task_path(repo, trial, job_dir)
    return build_feedback(
        repo_root=repo,
        task_path=task_path,
        trial_path=trial,
        max_chars=max_chars,
    )
