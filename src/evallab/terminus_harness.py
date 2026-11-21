from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evallab.evidence_store import evidence_tree_digest

CONFIG_PATH = "terminus/config.json"
RULES_PATH = "terminus/AGENTS.md"
SKILL_ROOT = "terminus/skills"
COMMAND_ROOT = "terminus-commands"
CONTEXT_ROOT = "terminus/context/"
RUNTIME_PREFIXES = ("terminus/sessions/", "terminus/trials/")

ALLOWED_KNOBS = frozenset(
    {
        "completion_fix",
        "enable_summarize",
        "interleaved_thinking",
        "llm_call_kwargs",
        "loop_break",
        "loop_command_run_min",
        "loop_grace_calls",
        "loop_message_run_min",
        "max_thinking_tokens",
        "max_turns",
        "output_cap_chars",
        "parser_name",
        "proactive_summarization_threshold",
        "reasoning_effort",
        "temperature",
        "trajectory_config",
    }
)

TRAJECTORY_CONFIG_KEYS = frozenset({"raw_content", "linear_history"})

_EXPLICIT_BINDING_KEYS = frozenset(
    {
        "model_name",
        "api_base",
        "llm_kwargs",
        "llm_backend",
        "model",
        "custom_llm_provider",
        "provider",
        "extra_headers",
        "headers",
    }
)
_BINDING_SUBSTRINGS = ("api_key", "api_base", "base_url", "credential", "secret")

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _is_binding_key(key: str) -> bool:
    folded = str(key).casefold()
    if folded in _EXPLICIT_BINDING_KEYS:
        return True
    if "model" in folded:
        return True
    return any(fragment in folded for fragment in _BINDING_SUBSTRINGS)


def _validate_config(config: dict[str, Any]) -> None:
    bound = sorted(str(key) for key in config if _is_binding_key(str(key)))
    if bound:
        raise ValueError(
            "terminus config "
            + ", ".join(repr(key) for key in bound)
            + " is model/transport binding, not candidate behavior: "
            "candidates vary Terminus behavior, never provider routing"
        )
    unknown = sorted(set(config) - ALLOWED_KNOBS)
    if unknown:
        raise ValueError(
            "terminus config sets keys that are not Terminus 2 arguments: " + ", ".join(unknown)
        )
    turns = config.get("max_turns")
    if turns is not None and (isinstance(turns, bool) or not isinstance(turns, int) or turns < 1):
        raise ValueError("terminus config max_turns must be a positive integer")
    if "trajectory_config" in config:
        trajectory = config["trajectory_config"]
        if not isinstance(trajectory, dict):
            raise ValueError("terminus config trajectory_config must be an object")
        unknown_trajectory = sorted(set(trajectory) - TRAJECTORY_CONFIG_KEYS)
        if unknown_trajectory:
            raise ValueError(
                "terminus config trajectory_config sets unknown keys "
                + ", ".join(unknown_trajectory)
                + ": only raw_content and linear_history are admitted"
            )
        non_boolean = sorted(
            str(key) for key, value in trajectory.items() if not isinstance(value, bool)
        )
        if non_boolean:
            raise ValueError(
                "terminus config trajectory_config values must be booleans: "
                + ", ".join(non_boolean)
            )
    if "llm_call_kwargs" in config:
        nested = config["llm_call_kwargs"]
        if not isinstance(nested, dict):
            raise ValueError("terminus config llm_call_kwargs must be an object")
        nested_bound = sorted(str(key) for key in nested if _is_binding_key(str(key)))
        if nested_bound:
            raise ValueError(
                "terminus llm_call_kwargs "
                + ", ".join(repr(key) for key in nested_bound)
                + " is model/transport binding, not candidate behavior: "
                "candidates vary Terminus behavior, never provider routing"
            )
    loop_break = config.get("loop_break")
    if loop_break is not None and not isinstance(loop_break, bool):
        raise ValueError("terminus config loop_break must be a boolean")
    completion_fix = config.get("completion_fix")
    if completion_fix is not None and not isinstance(completion_fix, bool):
        raise ValueError("terminus config completion_fix must be a boolean")
    cap = config.get("output_cap_chars")
    if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or cap < 2):
        raise ValueError("terminus config output_cap_chars must be an integer of at least 2")
    for knob in ("loop_command_run_min", "loop_message_run_min", "loop_grace_calls"):
        value = config.get(knob)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 2
        ):
            raise ValueError(f"terminus config {knob} must be an integer of at least 2")


@dataclass(frozen=True)
class TerminusHarnessTree:

    root: Path
    sha256: str
    config: dict[str, Any] = field(compare=False)
    rules_path: Path | None
    skill_roots: tuple[Path, ...]


def _resolve_source(source_path: Path | str, repo_root: Path | None) -> Path:
    raw = Path(source_path)
    if raw.is_symlink():
        raise ValueError(f"harness tree source must not be a symlink: {raw}")
    root = raw.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"harness tree root is not a directory: {root}")
    if repo_root is not None:
        anchor = Path(repo_root).resolve()
        if not root.is_relative_to(anchor):
            raise ValueError("harness tree path escapes repository root")
    return root


def _collect_files(root: Path) -> list[tuple[str, Path]]:
    entries = sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
    files: list[tuple[str, Path]] = []
    for path in entries:
        if path.is_symlink():
            raise ValueError(
                "harness tree contains unsupported symlink: " + path.relative_to(root).as_posix()
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(
                "harness tree contains unsupported special file: "
                + path.relative_to(root).as_posix()
            )
        files.append((path.relative_to(root).as_posix(), path))
    return files


def _check_fixed_paths(root: Path) -> None:
    for fixed in (CONFIG_PATH, RULES_PATH):
        candidate = root / fixed
        if candidate.is_symlink():
            raise ValueError(f"harness tree contains unsupported symlink: {fixed}")
        if candidate.exists() and not candidate.is_file():
            raise ValueError(f"harness tree {fixed} must be a regular file")


def _mapping(
    root: Path, blobs: dict[str, bytes]
) -> tuple[dict[str, Any], Path | None, tuple[Path, ...], str | None, list[str]]:
    for relative in blobs:
        if relative == "terminus/context" or relative.startswith(CONTEXT_ROOT):
            raise ValueError(
                f"terminus code_extension at {relative!r} is not supported: "
                "Eval Lab never imports candidate code"
            )
    for relative in blobs:
        if relative.startswith(RUNTIME_PREFIXES):
            raise ValueError(
                f"harness tree contains reserved runtime path {relative!r}: "
                "sessions/trials are written during runs, never pinned"
            )
    raw_config = blobs.get(CONFIG_PATH)
    if raw_config is None:
        config: dict[str, Any] = {}
    else:
        try:
            text = raw_config.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("terminus/config.json must be valid UTF-8") from exc
        try:
            config = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"terminus/config.json is not valid JSON: {exc}") from exc
        if not isinstance(config, dict):
            raise ValueError("terminus/config.json must be an object")
        _validate_config(config)
    rules_path: Path | None = None
    rules_relative: str | None = None
    raw_rules = blobs.get(RULES_PATH)
    if raw_rules is not None:
        try:
            rules_text = raw_rules.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("terminus/AGENTS.md must be valid UTF-8") from exc
        if rules_text.strip():
            rules_relative = RULES_PATH
            rules_path = root / RULES_PATH
    skill_roots: list[Path] = []
    skill_relatives: list[str] = []
    for prefix in (SKILL_ROOT, COMMAND_ROOT):
        if any(key.startswith(prefix + "/") and key.endswith("/SKILL.md") for key in blobs):
            skill_roots.append(root / prefix)
            skill_relatives.append(prefix)
    return config, rules_path, tuple(skill_roots), rules_relative, skill_relatives


def _check_digest(actual: str, expected: str | None, *, label: str) -> None:
    if expected is None:
        return
    if not _SHA256_RE.match(expected):
        raise ValueError(f"harness tree expected digest is malformed: {expected!r}")
    if actual != expected:
        raise ValueError(
            f"harness tree digest mismatch {label}: expected {expected} actual {actual}"
        )


def _read_blobs(root: Path, files: list[tuple[str, Path]]) -> dict[str, bytes]:
    blobs: dict[str, bytes] = {}
    for relative, path in files:
        try:
            blobs[relative] = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"harness tree file unavailable: {relative}: {exc}") from exc
    return blobs


def load_harness_tree(
    source_path: Path | str,
    expected_sha256: str | None = None,
    *,
    repo_root: Path | None = None,
) -> TerminusHarnessTree:
    root = _resolve_source(source_path, repo_root)
    files = _collect_files(root)
    _check_fixed_paths(root)
    digest = evidence_tree_digest(root)
    _check_digest(digest, expected_sha256, label="at load")
    blobs = _read_blobs(root, files)
    config, rules_path, skill_roots, _, _ = _mapping(root, blobs)
    return TerminusHarnessTree(
        root=root,
        sha256=digest,
        config=copy.deepcopy(config),
        rules_path=rules_path,
        skill_roots=skill_roots,
    )


def _publish_tree(blobs: dict[str, bytes], destination: Path) -> None:
    for relative in blobs:
        if not relative or relative.startswith("/") or ".." in Path(relative).parts:
            raise ValueError(f"harness tree refuses unsafe relative path: {relative!r}")
    if any(path.is_symlink() for path in (destination, *destination.parents)):
        raise ValueError(f"harness tree destination must not contain a symlink: {destination}")
    if destination.exists():
        if not destination.is_dir():
            raise ValueError(
                f"immutable harness tree destination is not a directory: {destination}"
            )
        staged = _collect_files(destination)
        existing = _read_blobs(destination, staged)
        if set(existing) != set(blobs):
            raise ValueError(
                "immutable harness tree destination has unexpected files: "
                f"expected {sorted(blobs)} actual {sorted(existing)}"
            )
        for relative, data in blobs.items():
            if existing[relative] != data:
                raise ValueError(f"immutable harness tree content mismatch: {relative}")
        return
    destination.mkdir(parents=True, exist_ok=True)
    for relative in sorted(blobs):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(blobs[relative])
        target.chmod(0o444)


def _base_metadata(
    digest: str,
    config: dict[str, Any],
    rules_relative: str | None,
    skill_relatives: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "sha256": digest,
        "artifact_path": "harness-tree",
        "config": copy.deepcopy(config),
        "rules_path": rules_relative,
        "skill_roots": list(skill_relatives),
    }


def stage_harness_tree(
    source_path: Path | str,
    expected_sha256: str,
    *,
    staging_root: Path,
    repo_root: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    if not expected_sha256 or not _SHA256_RE.match(expected_sha256):
        raise ValueError(f"harness tree expected digest is malformed: {expected_sha256!r}")
    root = _resolve_source(source_path, repo_root)
    files = _collect_files(root)
    _check_fixed_paths(root)
    digest = evidence_tree_digest(root)
    _check_digest(digest, expected_sha256, label="at stage")
    blobs = _read_blobs(root, files)
    config, _, _, rules_relative, skill_relatives = _mapping(root, blobs)
    staging_base = Path(staging_root)
    staged = staging_base / digest.removeprefix("sha256:")
    _publish_tree(blobs, staged)
    if evidence_tree_digest(staged) != digest:
        raise ValueError("staged harness tree digest drifted during copy")
    return staged, _base_metadata(digest, config, rules_relative, skill_relatives)


def retain_harness_tree_evidence(
    job_dir: Path,
    staged_tree: Path,
    metadata: dict[str, Any],
) -> Path:
    staged = Path(staged_tree)
    if staged.is_symlink():
        raise ValueError(f"staged harness tree must not be a symlink: {staged}")
    if not staged.is_dir():
        raise FileNotFoundError(f"staged harness tree is not a directory: {staged}")
    try:
        pinned = metadata["sha256"]
        claimed_config = metadata["config"]
        claimed_rules = metadata["rules_path"]
        claimed_skills = metadata["skill_roots"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"harness tree metadata is missing required fields: {exc}") from exc
    if metadata.get("schema_version") != 1:
        raise ValueError("harness tree metadata schema_version must be 1")
    if metadata.get("artifact_path") != "harness-tree":
        raise ValueError("harness tree metadata artifact_path must be 'harness-tree'")
    if not isinstance(pinned, str) or not _SHA256_RE.match(pinned):
        raise ValueError(f"harness tree metadata sha256 is malformed: {pinned!r}")
    actual = evidence_tree_digest(staged)
    if actual != pinned:
        raise ValueError(
            f"staged harness tree changed before evidence retention: "
            f"expected {pinned} actual {actual}"
        )
    staged_files = _collect_files(staged)
    _check_fixed_paths(staged)
    staged_blobs = _read_blobs(staged, staged_files)
    config, _, _, rules_relative, skill_relatives = _mapping(staged, staged_blobs)
    if not isinstance(claimed_config, dict) or claimed_config != config:
        raise ValueError("harness tree metadata config does not match staged bytes")
    if claimed_rules != rules_relative:
        raise ValueError("harness tree metadata rules_path does not match staged bytes")
    if not isinstance(claimed_skills, list) or claimed_skills != skill_relatives:
        raise ValueError("harness tree metadata skill_roots does not match staged bytes")
    target = Path(job_dir) / "harness-tree"
    _publish_tree(staged_blobs, target)
    if evidence_tree_digest(target) != pinned:
        raise ValueError("retained harness tree digest drifted during copy")
    return target
