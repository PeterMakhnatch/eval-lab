from __future__ import annotations

import hashlib
import json
import math
import random
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from evallab.evidence.facts import TrialFact, digest_json, extract_trial_fact
from evallab.evidence_store import evidence_tree_digest
from evallab.results import JobRecord, TrialRecord, load_job, load_jobs
from evallab.schemas import CohortComparisonSpec, CohortSelector

CONSEQUENTIAL_FIELDS = (
    "task_digest",
    "verifier_digest",
    "environment_digest",
    "agent_name",
    "agent_version",
    "model_name",
    "model_settings_digest",
    "preamble_hash",
    "preamble_content_sha256",
    "toolset_digest",
    "factor_values_digest",
    "factor_bindings_digest",
    "bound_execution_values_digest",
    "harness_tree_sha256",
    "harness_execution_settings_digest",
    "harness_base_agent_kwargs_digest",
)

# Retained Terminus harness-tree binding, per the HAR-71 implementation
# contract: ``lab-metadata.json.harness_tree`` names the retained bytes under
# ``harness-tree/`` and records the exact base/rendered bindings the runner
# used. Comparison treats the pinned tree as the causal treatment only after
# the retained bytes, digest, and rendered bindings all verify.
HARNESS_TREE_METADATA_KEY = "harness_tree"
HARNESS_TREE_RETAINED_DIR = "harness-tree"
HARNESS_TREE_SCHEMA_VERSION = 1
HARNESS_TREE_CONFIG_PATH = "terminus/config.json"
HARNESS_TREE_RULES_PATH = "terminus/AGENTS.md"
HARNESS_TREE_SKILL_ROOTS = ("terminus/skills", "terminus-commands")
HARNESS_TREE_BEHAVIOR_KNOBS = frozenset(
    {
        "enable_summarize",
        "interleaved_thinking",
        "llm_call_kwargs",
        "max_thinking_tokens",
        "max_turns",
        "parser_name",
        "proactive_summarization_threshold",
        "reasoning_effort",
        "temperature",
    }
)
HARNESS_TREE_RUNNER_FIELDS = (
    "base_agent_kwargs",
    "rendered_agent_kwargs",
    "rendered_rule_paths",
    "rendered_skill_paths",
    "execution_settings",
)
COST_BASIS_RECORDED_NATIVE = (
    "recorded execution cost from retained native evidence; "
    "provider-reported estimates are not invoices"
)

BOOTSTRAP_RESAMPLES = 4_000
NOT_COMPARABLE = "not distinguishable / not comparable"
TIMEOUT_BUDGET_EXCEPTION_CLASSES = frozenset(
    {"AgentTimeoutError", "TimeoutError", "TrialTimeoutFailure"}
)


@dataclass(frozen=True)
class CohortMember:
    cohort: str
    experiment_id: str
    job_id: str
    trial_id: str
    source_path: str
    trial_name: str
    task_name: str | None
    task_digest: str | None
    verifier_digest: str
    environment_digest: str
    grid_id: str | None
    point_id: str | None
    arm_id: str | None
    factor_values_json: str | None
    factor_values_digest: str | None
    factor_bindings_json: str | None
    factor_bindings_digest: str | None
    bound_execution_values_json: str | None
    bound_execution_values_digest: str | None
    preamble_path: str | None
    preamble_content_sha256: str | None
    task_family: str | None
    task_id: str | None
    task_instance_id: str | None
    generator_seed_json: str | None
    task_block_inputs_json: str | None
    task_block_id: str | None
    agent_name: str | None
    agent_version: str | None
    model_name: str | None
    model_settings_digest: str
    preamble_hash: str | None
    toolset: dict[str, Any] | None
    toolset_digest: str | None
    harness_policy_digest: str
    simulator_digest: str
    reward: float | None
    exception_class: str | None
    duration_seconds: float | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    tool_call_count: int
    started_at: str | None
    harness_tree_sha256: str | None
    harness_binding_problem: str | None
    harness_execution_settings_digest: str | None
    harness_base_agent_kwargs_digest: str | None
    harness_model_settings_digest: str | None
    harness_toolset_digest: str | None

    def condition(self, field: str) -> str | None:
        value = getattr(self, field)
        return str(value) if value is not None else None


def load_spec(path: Path) -> CohortComparisonSpec:
    try:
        return CohortComparisonSpec.model_validate_json(path.read_text())
    except (OSError, ValidationError) as exc:
        raise ValueError(f"Invalid cohort comparison spec {path}: {exc}") from exc


def wilson_interval(
    successes: int,
    denominator: int,
    z: float = 1.959963984540054,
) -> tuple[float, float] | None:
    if denominator == 0:
        return None
    proportion = successes / denominator
    z_squared = z * z
    scale = 1 + z_squared / denominator
    center = (proportion + z_squared / (2 * denominator)) / scale
    half_width = (
        z
        * math.sqrt(
            proportion * (1 - proportion) / denominator
            + z_squared / (4 * denominator * denominator)
        )
        / scale
    )
    lower = max(0.0, center - half_width)
    upper = min(1.0, center + half_width)
    return lower, upper


def _safe_path(root: Path, value: str) -> Path:
    path = (root / value).resolve()
    resolved_root = root.resolve()
    if path != resolved_root and resolved_root not in path.parents:
        raise ValueError(f"cohort path escapes repository: {value}")
    return path


def _selected_trials(root: Path, selector: CohortSelector) -> list[tuple[JobRecord, TrialRecord]]:
    selected: dict[str, tuple[JobRecord, TrialRecord]] = {}
    requested_names = set(selector.trial_names)
    for raw_path in selector.paths:
        path = _safe_path(root, raw_path)
        if (path / "result.json").is_file():
            value = json.loads((path / "result.json").read_text())
            if isinstance(value, dict) and "trial_name" in value and "task_name" in value:
                job = load_job(path.parent)
                jobs = [job]
                requested_names.add(path.name)
            else:
                jobs = [load_job(path)]
        else:
            jobs = load_jobs([path])
        for job in jobs:
            for trial in job.trials:
                if requested_names and trial.name not in requested_names:
                    continue
                selected[trial.id] = (job, trial)
    if not selected:
        raise ValueError(f"cohort {selector.label!r} selected no completed trials")
    return [selected[key] for key in sorted(selected)]


def _json_object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_started_at(value: str | None) -> datetime | None:
    """Parse a Harbor attempt timestamp; timezone-naive values are invalid."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _require_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _named_items(value: Any) -> list[str]:
    if isinstance(value, dict):
        return sorted(str(key) for key in value)
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            name = item.get("name") or item.get("id") or item.get("type")
            names.append(str(name) if name is not None else digest_json(item))
        else:
            names.append(str(item))
    return sorted(names)


def _configured_toolset(
    agent_name: str | None,
    agent_lock: dict[str, Any],
    trial_lock: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if not agent_name:
        return None, None
    kwargs = _json_object(agent_lock.get("kwargs"))
    tool_overrides = sorted(
        str(key) for key in kwargs if "tool" in str(key).lower() or "command" in str(key).lower()
    )
    toolset = {
        "profile": f"{agent_name}-default",
        "skills": _named_items(agent_lock.get("skills") or trial_lock.get("skills")),
        "mcp_servers": _named_items(agent_lock.get("mcp_servers")),
        "tool_override_keys": tool_overrides,
    }
    return toolset, digest_json(toolset)


_CONTENT_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def _valid_content_digest(value: Any) -> bool:
    return isinstance(value, str) and _CONTENT_DIGEST_PATTERN.fullmatch(value) is not None


def _path_key(value: str) -> str:
    """Normalize separators and dots without resolving symlink-sensitive parents."""
    return PurePosixPath(value).as_posix()

@dataclass(frozen=True)
class HarnessTreeBinding:
    """One job's retained harness-tree binding after full evidence verification."""

    sha256: str
    base_agent_kwargs: dict[str, Any]
    rendered_agent_kwargs: dict[str, Any]
    rendered_skill_paths: tuple[str, ...]
    execution_settings: dict[str, Any]
    rules_content_sha256: str | None
    skill_identities: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class VerifiedHarnessIdentities:
    """Tree-free identities used to normalize verified inductions away."""

    execution_settings_digest: str
    base_agent_kwargs_digest: str
    model_settings_digest: str
    toolset_digest: str | None


def _tree_relative_path(value: Any) -> PurePosixPath | None:
    """A relative posix path that stays inside the retained tree, or ``None``."""
    if not isinstance(value, str) or not value.strip():
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        return None
    return path


def _skill_directory_digest(skill_dir: Path) -> str:
    """Harbor's native skill digest (``harbor.skills.compute_skill_digest``).

    Sorted relative names and content digests, framed with NUL bytes; mirrors
    the upstream algorithm exactly, the way ``evallab.toolbox`` mirrors the
    two-file toolbox variant.
    """
    hasher = hashlib.sha256()
    for path in sorted(item for item in skill_dir.rglob("*") if item.is_file()):
        hasher.update(path.relative_to(skill_dir).as_posix().encode())
        hasher.update(b"\0")
        hasher.update(hashlib.sha256(path.read_bytes()).hexdigest().encode())
        hasher.update(b"\0")
    return f"sha256:{hasher.hexdigest()}"


def _tree_skill_identities(
    tree: Path, rendered_skill_paths: tuple[str, ...]
) -> tuple[dict[str, str], ...] | None:
    """``(name, digest)`` per skill Harbor loads from the retained tree.

    Mirrors ``harbor.skills._find_skill_dirs``: a rendered root is itself a
    skill when it carries ``SKILL.md``, otherwise each non-dot child directory
    must. Duplicate names resolve last-wins in render order, and the result is
    sorted by name. ``None`` means the retained root is malformed.
    """
    resolved: dict[str, str] = {}
    try:
        for raw_root in rendered_skill_paths:
            root_dir = tree / Path(raw_root)
            if (root_dir / "SKILL.md").is_file():
                skill_dirs = [root_dir]
            else:
                children = sorted(
                    (child for child in root_dir.iterdir() if child.is_dir()),
                    key=lambda child: child.name,
                )
                if not children or any(
                    not (child / "SKILL.md").is_file()
                    for child in children
                    if not child.name.startswith(".")
                ):
                    return None
                skill_dirs = [child for child in children if not child.name.startswith(".")]
            for skill_dir in skill_dirs:
                resolved[skill_dir.name] = _skill_directory_digest(skill_dir)
    except OSError:
        return None
    return tuple(
        {"name": name, "digest": digest} for name, digest in sorted(resolved.items())
    )


def _recorded_harness_binding(job: JobRecord) -> tuple[HarnessTreeBinding | None, str | None]:
    """Verify one job's recorded harness-tree binding from immutable evidence.

    Only the job's retained bytes (``harness-tree/``) and ``lab-metadata.json``
    are consulted; the present-day candidate tree never relabels a completed
    job. ``(None, None)`` means the job pinned no tree. A problem string means
    a binding was recorded but cannot be trusted — unknown schema, missing
    runner-recorded fields, tampered retained bytes, or incoherent rendered
    bindings — and such a job can never join a harness-tree comparison.
    """
    recorded = job.metadata.get(HARNESS_TREE_METADATA_KEY)
    if recorded is None:
        return None, None
    if not isinstance(recorded, dict):
        return None, "harness_tree metadata is not an object"
    if recorded.get("schema_version") != HARNESS_TREE_SCHEMA_VERSION:
        return None, (
            f"harness_tree schema_version {recorded.get('schema_version')!r} is unsupported"
        )
    sha256 = recorded.get("sha256")
    if not _valid_content_digest(sha256):
        return None, "harness_tree sha256 is missing or malformed"
    if recorded.get("artifact_path") != HARNESS_TREE_RETAINED_DIR:
        return None, (
            f"harness_tree artifact_path {recorded.get('artifact_path')!r} is not the retained tree"
        )
    config = recorded.get("config")
    if not isinstance(config, dict):
        return None, "harness_tree config is not an object"
    unknown_knobs = sorted(
        str(key) for key in config if str(key) not in HARNESS_TREE_BEHAVIOR_KNOBS
    )
    if unknown_knobs:
        return None, f"harness_tree config carries non-behavior keys {unknown_knobs}"
    rules_relative = _tree_relative_path(recorded.get("rules_path"))
    if recorded.get("rules_path") is not None and rules_relative is None:
        return None, f"harness_tree rules_path {recorded.get('rules_path')!r} escapes the tree"
    if rules_relative is not None and rules_relative.as_posix() != HARNESS_TREE_RULES_PATH:
        return None, (
            f"harness_tree rules_path {rules_relative.as_posix()!r} is not the tree rules file"
        )
    raw_skill_roots = recorded.get("skill_roots")
    if not isinstance(raw_skill_roots, list):
        return None, "harness_tree skill_roots is not a list"
    skill_roots: list[PurePosixPath] = []
    for raw_root in raw_skill_roots:
        root = _tree_relative_path(raw_root)
        if root is None or root.as_posix() not in HARNESS_TREE_SKILL_ROOTS:
            return None, f"harness_tree skill_root {raw_root!r} is not a tree skill root"
        skill_roots.append(root)
    runner_values: dict[str, Any] = {}
    for field in HARNESS_TREE_RUNNER_FIELDS:
        value = recorded.get(field)
        if value is None:
            return None, f"harness_tree metadata is missing recorded {field!r}"
        runner_values[field] = value
    base_agent_kwargs = runner_values["base_agent_kwargs"]
    rendered_agent_kwargs = runner_values["rendered_agent_kwargs"]
    execution_settings = runner_values["execution_settings"]
    rendered_rule_paths = runner_values["rendered_rule_paths"]
    rendered_skill_paths = runner_values["rendered_skill_paths"]
    if not isinstance(base_agent_kwargs, dict) or not isinstance(rendered_agent_kwargs, dict):
        return None, "harness_tree agent kwargs records are not objects"
    if not isinstance(execution_settings, dict):
        return None, "harness_tree execution_settings is not an object"
    if not isinstance(rendered_rule_paths, list) or any(
        not isinstance(item, str) for item in rendered_rule_paths
    ):
        return None, "harness_tree rendered_rule_paths is not a list of paths"
    if not isinstance(rendered_skill_paths, list) or any(
        not isinstance(item, str) for item in rendered_skill_paths
    ):
        return None, "harness_tree rendered_skill_paths is not a list of paths"

    tree = job.path / HARNESS_TREE_RETAINED_DIR
    job_root = job.path.resolve()
    if tree.is_symlink() or not tree.is_dir() or not tree.resolve().is_relative_to(job_root):
        return None, "retained harness tree is missing or escapes the job directory"
    try:
        retained_digest = evidence_tree_digest(tree)
        tree_config = json.loads((tree / HARNESS_TREE_CONFIG_PATH).read_text())
    except (OSError, ValueError, json.JSONDecodeError, UnicodeError):
        return None, "retained harness tree bytes cannot be read"
    if retained_digest != sha256:
        return None, "retained harness tree digest does not match the recorded binding"
    if not isinstance(tree_config, dict) or tree_config != config:
        return None, "retained tree config does not match the recorded config"

    expected_kwargs = dict(base_agent_kwargs)
    for key, value in config.items():
        if key == "llm_call_kwargs" and isinstance(value, dict):
            expected_kwargs[key] = {**_json_object(base_agent_kwargs.get(key)), **value}
        else:
            expected_kwargs[key] = value
    if rendered_agent_kwargs != expected_kwargs:
        return None, (
            "rendered agent kwargs do not equal the base kwargs overridden by the tree config"
        )

    rules_bytes = b""
    if rules_relative is not None:
        rules_file = tree / Path(rules_relative)
        if not rules_file.is_file() or rules_file.is_symlink():
            return None, "recorded rules_path is not a regular file in the retained tree"
        rules_bytes = rules_file.read_bytes()
    expected_rule_paths = [HARNESS_TREE_RULES_PATH] if rules_bytes.strip() else []
    if list(rendered_rule_paths) != expected_rule_paths:
        return None, "rendered rule paths do not match the retained tree rules"

    expected_skill_roots = sorted(
        root.as_posix()
        for root in skill_roots
        if any(
            path.name == "SKILL.md" and path.is_file()
            for path in (tree / Path(root)).rglob("*")
        )
    )
    if sorted(rendered_skill_paths) != expected_skill_roots:
        return None, "rendered skill paths do not match the skill roots in the retained tree"
    skill_identities = _tree_skill_identities(tree, tuple(rendered_skill_paths))
    if skill_identities is None:
        return None, "retained tree skill roots are malformed"
    return (
        HarnessTreeBinding(
            sha256=sha256,
            base_agent_kwargs=base_agent_kwargs,
            rendered_agent_kwargs=rendered_agent_kwargs,
            rendered_skill_paths=tuple(rendered_skill_paths),
            execution_settings=execution_settings,
            rules_content_sha256=(
                "sha256:" + hashlib.sha256(rules_bytes).hexdigest()
                if rules_bytes.strip()
                else None
            ),
            skill_identities=skill_identities,
        ),
        None,
    )


def _frozen_skill_identities(trial: TrialRecord) -> list[tuple[str, str]] | None:
    """``(name, digest)`` pairs frozen in one trial's resolved skill locks."""
    skills = trial.lock.get("skills")
    if skills in (None, []):
        return []
    if not isinstance(skills, list):
        return None
    identities: list[tuple[str, str]] = []
    for skill in skills:
        if not isinstance(skill, dict):
            return None
        name = skill.get("name")
        digest = skill.get("digest")
        if not isinstance(name, str) or not _valid_content_digest(digest):
            return None
        identities.append((name, digest))
    return sorted(identities)


def _rendered_skill_path_matches(actual: Any, rendered: str) -> bool:
    """One frozen agent skill entry against its tree-relative rendered path.

    The runner may pass either the tree-relative path or the staged absolute
    path to Harbor; both freeze to strings that end with the rendered relative
    path, which is the verifiable invariant either way.
    """
    if not isinstance(actual, str):
        return False
    actual_key = _path_key(actual)
    return actual_key == rendered or actual_key.endswith("/" + rendered)


def _verified_harness_identities(
    binding: HarnessTreeBinding,
    trial: TrialRecord,
    fact: TrialFact,
    agent_lock: dict[str, Any],
    model_settings: dict[str, Any],
    toolset: dict[str, Any] | None,
    preamble_hash: str | None,
) -> tuple[VerifiedHarnessIdentities | None, str | None]:
    """Verify the trial's frozen bindings are exactly the rendered tree.

    The pinned tree may induce — and only induce — differences in agent
    kwargs, extra instructions, and skills. Each frozen surface is compared
    against the recorded rendering before it is normalized away; any other
    difference (an independent preamble, skills from outside the tree,
    kwargs the tree did not render) fails verification, and the caller keeps
    the raw consequential identities so the cohorts stay not comparable.
    """
    if _json_object(agent_lock.get("kwargs")) != binding.rendered_agent_kwargs:
        return None, "frozen agent kwargs do not equal the recorded rendered kwargs"
    if "skills" in agent_lock and not (
        isinstance(agent_lock["skills"], list)
        and len(agent_lock["skills"]) == len(binding.rendered_skill_paths)
        and all(
            _rendered_skill_path_matches(actual, rendered)
            for actual, rendered in zip(
                agent_lock["skills"], binding.rendered_skill_paths, strict=True
            )
        )
    ):
        return None, "frozen agent skills do not match the rendered skill paths"
    frozen_skills = _frozen_skill_identities(trial)
    expected_skills = sorted((item["name"], item["digest"]) for item in binding.skill_identities)
    if frozen_skills is None or frozen_skills != expected_skills:
        return None, "frozen skills do not match the retained tree skills"
    if fact.preamble_path is not None or fact.preamble_content_sha256 is not None:
        return None, "queue-recorded independent preamble is present"
    if binding.rules_content_sha256 is None:
        expected_preamble = digest_json({"preamble": "none"})
    else:
        expected_preamble = digest_json(
            {"inline": [], "files": [binding.rules_content_sha256]}
        )
    if preamble_hash is None:
        return None, "retained preamble identity is unknown"
    if preamble_hash != expected_preamble:
        return None, "retained preamble does not equal the rendered tree rules"
    # The frozen kwargs and skills were just verified to be exactly the
    # rendered tree, so their (possibly differing) content is the declared
    # treatment itself; every remaining surface must stay identical.
    normalized_settings = {
        key: value for key, value in model_settings.items() if key not in {"kwargs", "skills"}
    }
    normalized_toolset: dict[str, Any] | None = None
    if toolset is not None:
        normalized_toolset = {key: value for key, value in toolset.items() if key != "skills"}
    return (
        VerifiedHarnessIdentities(
            execution_settings_digest=digest_json(binding.execution_settings),
            base_agent_kwargs_digest=digest_json(binding.base_agent_kwargs),
            model_settings_digest=digest_json(normalized_settings),
            toolset_digest=digest_json(normalized_toolset)
            if normalized_toolset is not None
            else None,
        ),
        None,
    )


def _declared_instruction_files(source: dict[str, Any]) -> list[tuple[str, str | None]] | None:
    """Ordered ``(path, retained content digest)`` instruction files in a source.

    Harbor freezes each extra instruction file of a trial in the lock as an
    ``extra_instructions`` entry carrying the run-time content digest. A digest
    of ``None`` means the declaration survived without content provenance.
    ``None`` is returned for malformed declarations.
    """
    value = source.get("extra_instructions")
    if value in (None, "", []) or isinstance(value, str):
        # A bare string is inline preamble text classified by
        # ``_declared_inline_preambles``; only the ordered list/dict shapes
        # declare instruction files.
        return []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return None
    entries: list[tuple[str, str | None]] = []
    for item in value:
        if isinstance(item, str):
            if not item.strip():
                return None
            entries.append((item, None))
            continue
        if not isinstance(item, dict):
            return None
        path = item.get("path")
        digest = item.get("digest")
        if not isinstance(path, str) or not path.strip():
            return None
        if digest is not None and not _valid_content_digest(digest):
            return None
        entries.append((path, digest))
    return entries


def _declared_instruction_paths(source: dict[str, Any]) -> list[str] | None:
    """Read ordered file declarations without conflating repeated instructions."""
    declared: list[str] = []
    kwargs = _json_object(_json_object(source.get("agent")).get("kwargs"))
    for holder in (source, kwargs):
        for name in ("extra_instruction_paths", "extra_instruction_path"):
            value = holder.get(name)
            if value in (None, "", []):
                continue
            values = [value] if isinstance(value, str) else value
            if not isinstance(values, list) or any(
                not isinstance(item, str) or not item.strip() for item in values
            ):
                return None
            paths = [_path_key(item) for item in values]
            if declared and paths != declared:
                return None
            declared = paths
    return declared


def _declared_inline_preambles(source: dict[str, Any]) -> list[tuple[str, str]] | None:
    """Inline preamble text declared in one retained source.

    ``extra_instructions`` values that are lists or dicts are file declarations
    classified by ``_declared_instruction_files`` instead. ``None`` is returned
    for unsupported declaration shapes.
    """
    entries: list[tuple[str, str]] = []
    kwargs = _json_object(_json_object(source.get("agent")).get("kwargs"))
    for holder in (source, kwargs):
        for key in ("preamble", "system_prompt"):
            value = holder.get(key)
            if value in (None, "", []):
                continue
            if not isinstance(value, str):
                return None
            entries.append((key, value))
        value = holder.get("extra_instructions")
        if isinstance(value, str) and value != "":
            entries.append(("extra_instructions", value))
        elif holder is kwargs and value not in (None, "", []):
            # Only Harbor's top-level frozen file entries have a defined
            # content-binding contract. Never erase an unmodeled declaration.
            return None
    return entries


def _retained_preamble_hash(trial: TrialRecord, fact: TrialFact) -> str | None:
    """Retained identity of a completed trial's effective extra preamble.

    Only immutable run evidence is consulted; the present-day filesystem is
    never read, so editing, moving, or removing an instruction file cannot
    relabel a completed trial. Precedence:

    * the queue's execution-time provenance (``fact.preamble_path`` and
      ``fact.preamble_content_sha256``, from ``lab-metadata.json``) — the
      singular producer's validated content digest;
    * Harbor's frozen lock ``extra_instructions`` entries, each carrying the
      run-time content digest of one ordered instruction file;
    * declared ``extra_instruction_paths`` and inline preamble text retained
      in the lock/result/config records.

    Conservative cases return ``None`` (identity UNKNOWN — never the
    no-preamble digest):

    * a declared instruction path without any retained content digest;
    * multiple ordered instruction files where any entry lacks a retained
      digest, because the singular provenance digest cannot certify the whole
      effective preamble;
    * contradictory retained digests (lock entries disagreeing with each other
      or with provenance) and malformed declaration shapes.

    The identity is content-based: declared paths only match declarations to
    digests and never enter the digest itself, so the same retained content
    compares equal under different paths.
    """
    sources = (trial.lock, _json_object(trial.result.get("config")), trial.config)
    file_order: list[str] = []
    retained_digests: dict[str, str] = {}
    for source in sources:
        files = _declared_instruction_files(source)
        paths = _declared_instruction_paths(source)
        if files is None or paths is None:
            return None
        for path, digest in files:
            key = _path_key(path)
            if digest is not None:
                previous = retained_digests.get(key)
                if previous is not None and previous != digest:
                    return None
                retained_digests[key] = digest
        for sequence in ([_path_key(path) for path, _ in files], paths):
            if not sequence:
                continue
            if file_order and sequence != file_order:
                return None
            file_order = sequence

    inline: list[tuple[str, str]] = []
    for source in sources:
        entries = _declared_inline_preambles(source)
        if entries is None:
            return None
        if entries:
            inline = entries
            break

    provenance_digest = fact.preamble_content_sha256
    if provenance_digest is not None and not _valid_content_digest(provenance_digest):
        return None

    file_digests: list[str]
    if not file_order:
        if fact.preamble_path is not None and provenance_digest is None:
            return None
        file_digests = [provenance_digest] if provenance_digest is not None else []
    else:
        file_digests = []
        provenance_path = _path_key(fact.preamble_path) if fact.preamble_path is not None else None
        for key in file_order:
            retained = retained_digests.get(key)
            if retained is None:
                # Without a lock digest, provenance must identify this exact
                # single file. Matching basenames cannot establish identity.
                if len(file_order) != 1 or key != provenance_path:
                    return None
                retained = provenance_digest
            if retained is None:
                return None
            if (
                provenance_digest is not None
                and (len(file_order) == 1 or key == provenance_path)
                and retained != provenance_digest
            ):
                return None
            file_digests.append(retained)
        if provenance_digest is not None and provenance_digest not in file_digests:
            return None

    if not inline and not file_digests:
        return digest_json({"preamble": "none"})
    return digest_json(
        {
            "inline": [{"kind": kind, "sha256": digest_json(text)} for kind, text in inline],
            "files": file_digests,
        }
    )


def _member(
    root: Path,
    experiment_id: str,
    label: str,
    job: JobRecord,
    trial: TrialRecord,
    reward_name: str,
) -> CohortMember:
    fact: TrialFact = extract_trial_fact(job, trial)
    agent_lock = _json_object(trial.lock.get("agent"))
    model_settings = {
        key: value
        for key, value in agent_lock.items()
        if key not in {"name", "model_name", "import_path"}
    }
    agent_name = fact.agent_name
    agent_version = fact.agent_version
    if agent_version is None and agent_lock.get("version") is not None:
        agent_version = str(agent_lock["version"])
    model_name = fact.model_name
    if model_name is None and agent_lock.get("model_name") is not None:
        model_name = str(agent_lock["model_name"])
    if model_name is None and agent_name in {"oracle", "nop"}:
        model_name = "not-applicable"
    toolset, toolset_digest = _configured_toolset(agent_name, agent_lock, trial.lock)
    experiment = _json_object(job.metadata.get("experiment"))
    if experiment.get("toolbox_path") is not None or "toolbox" in job.metadata:
        # Skill content is a toolset intervention, not a model sampling setting.
        model_settings.pop("skills", None)
        toolbox = _json_object(job.metadata.get("toolbox"))
        skills = trial.lock.get("skills")
        artifact_path = toolbox.get("artifact_path")
        known = (
            toolset is not None
            and _valid_content_digest(toolbox.get("sha256"))
            and toolbox.get("sha256") == experiment.get("toolbox_sha256")
            and _valid_content_digest(toolbox.get("skill_digest"))
            and isinstance(skills, list)
            and len(skills) == 1
            and isinstance(skills[0], dict)
            and skills[0].get("name") == "repl-tools"
            and skills[0].get("digest") == toolbox.get("skill_digest")
            and isinstance(artifact_path, str)
            and not Path(artifact_path).is_absolute()
        )
        if known:
            artifact = job.path / artifact_path
            known = (
                not artifact.is_symlink()
                and artifact.resolve().is_relative_to(job.path.resolve())
                and artifact.is_file()
                and "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
                == toolbox["sha256"]
            )
        if known:
            from evallab.toolbox import compute_skill_digest

            try:
                known = compute_skill_digest(artifact.parent) == toolbox["skill_digest"]
            except (OSError, ValueError):
                known = False
        if known and toolset is not None:
            toolset = {
                **toolset,
                "skills": [{"name": "repl-tools", "digest": toolbox["skill_digest"]}],
                "python_toolbox_sha256": toolbox["sha256"],
            }
            toolset_digest = digest_json(toolset)
        else:
            toolset, toolset_digest = None, None
    preamble_hash = _retained_preamble_hash(trial, fact)
    harness_fields = _harness_member_fields(
        job, trial, fact, agent_lock, model_settings, toolset, preamble_hash
    )
    try:
        source_path = trial.path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        source_path = trial.path.resolve().as_posix()
    return CohortMember(
        cohort=label,
        experiment_id=experiment_id,
        job_id=job.id,
        trial_id=trial.id,
        source_path=source_path,
        trial_name=trial.name,
        task_name=fact.task_name,
        task_digest=fact.task_digest,
        verifier_digest=fact.verifier_digest,
        environment_digest=fact.environment_digest,
        grid_id=fact.grid_id,
        point_id=fact.point_id,
        arm_id=fact.arm_id,
        factor_values_json=fact.factor_values_json,
        factor_values_digest=fact.factor_values_digest,
        factor_bindings_json=fact.factor_bindings_json,
        factor_bindings_digest=fact.factor_bindings_digest,
        bound_execution_values_json=fact.bound_execution_values_json,
        bound_execution_values_digest=fact.bound_execution_values_digest,
        preamble_path=fact.preamble_path,
        preamble_content_sha256=fact.preamble_content_sha256,
        task_family=fact.task_family,
        task_id=fact.task_id,
        task_instance_id=fact.task_instance_id,
        generator_seed_json=fact.generator_seed_json,
        task_block_inputs_json=fact.task_block_inputs_json,
        task_block_id=fact.task_block_id,
        agent_name=agent_name,
        agent_version=agent_version,
        model_name=model_name,
        model_settings_digest=digest_json(model_settings),
        preamble_hash=preamble_hash,
        toolset=toolset,
        toolset_digest=toolset_digest,
        harness_policy_digest=digest_json(
            {
                "harbor": job.lock.get("harbor") or {},
                "harness": trial.lock.get("harness") or {},
                "policy": trial.lock.get("policy") or {},
            }
        ),
        simulator_digest=digest_json(trial.lock.get("simulator") or {}),
        reward=trial.rewards.get(reward_name),
        exception_class=fact.exception_class,
        duration_seconds=fact.duration_seconds,
        input_tokens=fact.input_tokens,
        output_tokens=fact.output_tokens,
        cost_usd=fact.cost_usd,
        tool_call_count=fact.tool_call_count,
        started_at=_string_or_none(trial.result.get("started_at")),
        **harness_fields,
    )


def _harness_member_fields(
    job: JobRecord,
    trial: TrialRecord,
    fact: TrialFact,
    agent_lock: dict[str, Any],
    model_settings: dict[str, Any],
    toolset: dict[str, Any] | None,
    preamble_hash: str | None,
) -> dict[str, Any]:
    """CohortMember harness fields from the verified retained binding."""
    binding, problem = _recorded_harness_binding(job)
    if binding is None:
        return {
            "harness_tree_sha256": None,
            "harness_binding_problem": problem,
            "harness_execution_settings_digest": None,
            "harness_base_agent_kwargs_digest": None,
            "harness_model_settings_digest": None,
            "harness_toolset_digest": None,
        }
    identities, problem = _verified_harness_identities(
        binding, trial, fact, agent_lock, model_settings, toolset, preamble_hash
    )
    if identities is None:
        return {
            "harness_tree_sha256": None,
            "harness_binding_problem": problem,
            "harness_execution_settings_digest": None,
            "harness_base_agent_kwargs_digest": None,
            "harness_model_settings_digest": None,
            "harness_toolset_digest": None,
        }
    return {
        "harness_tree_sha256": binding.sha256,
        "harness_binding_problem": None,
        "harness_execution_settings_digest": identities.execution_settings_digest,
        "harness_base_agent_kwargs_digest": identities.base_agent_kwargs_digest,
        "harness_model_settings_digest": identities.model_settings_digest,
        "harness_toolset_digest": identities.toolset_digest,
    }


def assemble_members(root: Path, spec: CohortComparisonSpec) -> list[CohortMember]:
    members: list[CohortMember] = []
    owner_by_trial: dict[str, str] = {}
    for selector in spec.cohorts:
        for job, trial in _selected_trials(root, selector):
            previous = owner_by_trial.get(trial.id)
            if previous is not None and previous != selector.label:
                raise ValueError(
                    f"trial {trial.id} belongs to both {previous!r} and {selector.label!r}"
                )
            owner_by_trial[trial.id] = selector.label
            members.append(
                _member(
                    root,
                    spec.experiment_id,
                    selector.label,
                    job,
                    trial,
                    spec.reward_name,
                )
            )
    return sorted(members, key=lambda item: (item.cohort, item.task_digest or "", item.trial_id))


def _comparability_condition(
    member: CohortMember, field: str, declared_variable: str
) -> str | None:
    """Condition value with verified harness-tree inductions normalized away.

    Under a declared harness-tree treatment, the raw model-settings and
    toolset identities are replaced by their tree-free forms for members whose
    retained binding fully verified, so only unexplained differences remain
    consequential. Members with an unverified binding keep their raw
    identities and can never compare equal to a verified arm.
    """
    if declared_variable == "harness_tree_sha256" and member.harness_tree_sha256 is not None:
        if field == "model_settings_digest":
            return member.harness_model_settings_digest
        if field == "toolset_digest":
            return member.harness_toolset_digest
    return member.condition(field)


def _validate_comparability(spec: CohortComparisonSpec, members: list[CohortMember]) -> list[str]:
    observed = {
        field: sorted(
            {
                _comparability_condition(member, field, spec.declared_variable)
                for member in members
            },
            key=lambda value: "" if value is None else value,
        )
        for field in CONSEQUENTIAL_FIELDS
    }
    treatment_fields = {
        "agent_name",
        "agent_version",
        "model_name",
        "model_settings_digest",
        "environment_digest",
        "preamble_hash",
        "toolset_digest",
        "factor_values_digest",
        "bound_execution_values_digest",
        "factor_bindings_digest",
        "preamble_content_sha256",
        "harness_tree_sha256",
        "harness_execution_settings_digest",
        "harness_base_agent_kwargs_digest",
    }
    differing_fields = [field for field in treatment_fields if len(observed[field]) > 1]
    warnings: list[str] = []
    if spec.declared_variable in {
        "factor_values_digest",
        "bound_execution_values_digest",
    }:
        required = (
            "factor_values_digest",
            "factor_bindings_digest",
            "bound_execution_values_digest",
        )
        for field in required:
            if any(member.condition(field) is None for member in members):
                warnings.append(f"controlled factor provenance is missing {field!r}")
    if spec.declared_variable in {"preamble_hash", "preamble_content_sha256"} and any(
        member.preamble_path is not None and member.preamble_content_sha256 is None
        for member in members
    ):
        warnings.append("controlled preamble provenance is missing content sha256")
    if spec.declared_variable == "toolset_digest" and any(
        member.toolset_digest is None for member in members
    ):
        warnings.append(
            "controlled toolset identity is unknown (missing or conflicting retained evidence)"
        )
    if spec.declared_variable in {"preamble_hash", "preamble_content_sha256"} and any(
        member.preamble_hash is None for member in members
    ):
        warnings.append(
            "controlled preamble identity is unknown (missing or conflicting retained evidence)"
        )
    for field, expected in spec.constraints.items():
        actual = set(observed[field])
        if actual != {expected}:
            observed_values = sorted(str(value) for value in actual)
            warnings.append(
                f"constraint {field}={expected!r} does not match observed {observed_values}"
            )
    allowed_differences = {spec.declared_variable}
    if spec.declared_variable == "agent_name":
        allowed_differences.update(
            {"agent_version", "model_name", "model_settings_digest", "toolset_digest"}
        )
    elif spec.declared_variable == "model_name":
        allowed_differences.add("model_settings_digest")
    elif spec.declared_variable == "factor_values_digest":
        allowed_differences.add("bound_execution_values_digest")
    elif spec.declared_variable == "bound_execution_values_digest":
        allowed_differences.add("factor_values_digest")
    elif spec.declared_variable == "preamble_hash":
        allowed_differences.add("preamble_content_sha256")
    elif spec.declared_variable == "preamble_content_sha256":
        allowed_differences.add("preamble_hash")
    elif spec.declared_variable == "harness_tree_sha256":
        allowed_differences.update(
            {"model_settings_digest", "preamble_hash", "preamble_content_sha256", "toolset_digest"}
        )
        unverified = sorted(
            {
                member.harness_binding_problem or "no harness_tree binding is recorded"
                for member in members
                if member.harness_tree_sha256 is None
            }
        )
        if unverified:
            warnings.append(
                "harness binding is missing or unverified: " + "; ".join(unverified)
            )
    undeclared = [field for field in differing_fields if field not in allowed_differences]
    if spec.declared_variable not in differing_fields:
        warnings.append(f"declared variable {spec.declared_variable!r} does not differ")
    warnings.extend(
        f"undeclared consequential variable differs: {field} ({observed[field]})"
        for field in undeclared
    )
    by_task: dict[str, list[CohortMember]] = defaultdict(list)
    for member in members:
        key = _pairing_value(member, spec.pairing_key)
        if key is not None:
            by_task[key].append(member)
    invariants = {"task_digest", "verifier_digest"}
    if spec.declared_variable != "environment_digest":
        invariants.add("environment_digest")
    for task_key, task_members in sorted(by_task.items()):
        for field in sorted(invariants):
            values = sorted(
                {member.condition(field) for member in task_members},
                key=lambda value: "" if value is None else value,
            )
            if len(values) > 1:
                warnings.append(
                    f"causal invariant differs within task {task_key}: {field} ({values})"
                )
    return list(dict.fromkeys(warnings))


def _numeric_summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"n": 0, "min": None, "median": None, "mean": None, "max": None}
    return {
        "n": len(values),
        "min": min(values),
        "median": statistics.median(values),
        "mean": statistics.fmean(values),
        "max": max(values),
    }


def _pairing_value(member: CohortMember, pairing_key: str) -> str | None:
    value = getattr(member, pairing_key)
    return str(value) if value is not None else None


def _quantile(values: list[float], probability: float) -> float:
    if not values:
        raise ValueError("cannot take a quantile of no values")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def bootstrap_mean_interval(
    values: list[float],
    *,
    confidence: float = 0.95,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = 0,
) -> tuple[float, float] | None:
    """Percentile interval that resamples the supplied evidence units."""
    if not values:
        return None
    if not 0 < confidence < 1:
        raise ValueError("confidence must be between zero and one")
    if resamples < 1:
        raise ValueError("resamples must be positive")
    if len(values) == 1:
        return values[0], values[0]
    generator = random.Random(seed)
    count = len(values)
    means = [
        statistics.fmean(values[generator.randrange(count)] for _ in range(count))
        for _ in range(resamples)
    ]
    tail = (1 - confidence) / 2
    return _quantile(means, tail), _quantile(means, 1 - tail)


def _bootstrap_seed(*parts: object) -> int:
    payload = "\0".join(str(part) for part in parts).encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def pass_at_k_probability(attempt_probability: float, k: int) -> float:
    """Model-based independent-attempt power-planning transform, not an empirical estimator.

    This is ``1 - (1-p)**k``. It is not realized first-k (any/all over ordered attempts)
    and it is not the Chen/Yao combinatorial estimator computed from observed (n, c, k).
    """
    if not 0 <= attempt_probability <= 1:
        raise ValueError("attempt probability must be between zero and one")
    if k < 1:
        raise ValueError("k must be positive")
    return 1 - (1 - attempt_probability) ** k


def pass_at_k_unbiased(n: int, c: int, k: int) -> float | None:
    """Chen unbiased pass@k from per-task attempt counts: ``1 - C(n-c, k) / C(n, k)``.

    Returns None when ``n < k``. Does not use attempt order.
    """
    n = _require_int("n", n)
    c = _require_int("c", c)
    k = _require_int("k", k)
    if n < 0 or c < 0 or c > n or k < 1:
        raise ValueError("n, c, and k must satisfy n >= 0, 0 <= c <= n, and k >= 1")
    if n < k:
        return None
    if n - c < k:
        return 1.0
    denominator = math.comb(n, k)
    zero_success_numerator = math.comb(n - c, k)
    return float((denominator - zero_success_numerator) / denominator)


def pass_power_k_unbiased(n: int, c: int, k: int) -> float | None:
    """Yao/tau unbiased pass^k from per-task attempt counts: ``C(c, k) / C(n, k)``.

    Returns None when ``n < k``. Does not use attempt order.
    """
    n = _require_int("n", n)
    c = _require_int("c", c)
    k = _require_int("k", k)
    if n < 0 or c < 0 or c > n or k < 1:
        raise ValueError("n, c, and k must satisfy n >= 0, 0 <= c <= n, and k >= 1")
    if n < k:
        return None
    if c < k:
        return 0.0
    return math.comb(c, k) / math.comb(n, k)


def _normal_cutoff(alpha: float, target_power: float) -> float:
    if not 0 < alpha < 1:
        raise ValueError("alpha must be between zero and one")
    if not 0 < target_power < 1:
        raise ValueError("power must be between zero and one")
    normal = statistics.NormalDist()
    return normal.inv_cdf(1 - alpha / 2) + normal.inv_cdf(target_power)


def _paired_variance(p0: float, p1: float, correlation: float) -> float:
    if not -0.99 <= correlation <= 0.99:
        raise ValueError("pair correlation must be between -0.99 and 0.99")
    covariance = correlation * math.sqrt(p0 * (1 - p0) * p1 * (1 - p1))
    return max(p0 * (1 - p0) + p1 * (1 - p1) - 2 * covariance, 0.0)


def required_tasks_for_effect(
    *,
    baseline: float,
    attempt_effect: float,
    k: int,
    alpha: float = 0.05,
    target_power: float = 0.8,
    pair_correlation: float = 0.0,
) -> int | None:
    if attempt_effect <= 0 or baseline + attempt_effect > 1:
        raise ValueError("target effect must be positive and keep probability at or below one")
    p0 = pass_at_k_probability(baseline, k)
    p1 = pass_at_k_probability(baseline + attempt_effect, k)
    task_effect = p1 - p0
    if task_effect <= 0:
        return None
    variance = _paired_variance(p0, p1, pair_correlation)
    required = (_normal_cutoff(alpha, target_power) ** 2) * variance / (task_effect**2)
    return max(2, math.ceil(required))


def minimum_detectable_effect(
    *,
    n_tasks: int,
    k: int,
    baseline: float,
    alpha: float = 0.05,
    target_power: float = 0.8,
    pair_correlation: float = 0.0,
) -> float | None:
    if n_tasks < 2:
        raise ValueError("n_tasks must be at least two")
    if not 0 <= baseline < 1:
        raise ValueError("baseline must be at least zero and below one")

    def detectable(attempt_effect: float) -> bool:
        required = required_tasks_for_effect(
            baseline=baseline,
            attempt_effect=attempt_effect,
            k=k,
            alpha=alpha,
            target_power=target_power,
            pair_correlation=pair_correlation,
        )
        return required is not None and required <= n_tasks

    upper = 1 - baseline
    if not detectable(upper):
        return None
    lower = 0.0
    for _ in range(60):
        midpoint = (lower + upper) / 2
        if detectable(midpoint):
            upper = midpoint
        else:
            lower = midpoint
    return upper


def power_requirements(
    *,
    baseline: float,
    attempt_effect: float,
    max_k: int,
    alpha: float = 0.05,
    target_power: float = 0.8,
    pair_correlation: float = 0.0,
) -> list[dict[str, float | int | None]]:
    if max_k < 1:
        raise ValueError("max_k must be positive")
    rows: list[dict[str, float | int | None]] = []
    for k in range(1, max_k + 1):
        p0 = pass_at_k_probability(baseline, k)
        p1 = pass_at_k_probability(baseline + attempt_effect, k)
        n_tasks = required_tasks_for_effect(
            baseline=baseline,
            attempt_effect=attempt_effect,
            k=k,
            alpha=alpha,
            target_power=target_power,
            pair_correlation=pair_correlation,
        )
        rows.append(
            {
                "k": k,
                "baseline_pass_at_k": p0,
                "comparison_pass_at_k": p1,
                "task_level_effect": p1 - p0,
                "required_n_tasks": n_tasks,
                "total_attempts_two_cohorts": 2 * k * n_tasks if n_tasks else None,
            }
        )
    return rows


def design_effect(*, icc: float, cluster_size: int) -> float:
    """Between-cluster variance inflation factor ``DE = 1 + (cluster_size - 1) * icc``.

    Refuses an absent or invalid ICC: the intraclass correlation must be a finite
    value in ``[0, 1)``, and a cluster size below one is refused. ``icc == 0.0``
    (rho=0) yields ``DE == 1.0``, exactly preserving independent (unclustered)
    sizing so the clustered contract never inflates an already-independent plan.
    """
    if not math.isfinite(icc):
        raise ValueError("icc must be a finite number")
    if not 0.0 <= icc < 1.0:
        raise ValueError("icc must be at least zero and below one")
    cluster_size = _require_int("cluster_size", cluster_size)
    if cluster_size < 1:
        raise ValueError("cluster_size must be at least one")
    return 1.0 + (cluster_size - 1) * icc


def effective_sample_size(*, n_units: int, icc: float, cluster_size: int) -> float:
    """Number of independent evidence units carried by ``n_units`` clustered observations.

    ``n_units / design_effect(icc, cluster_size)``. Refuses absent/invalid ICC and
    sub-one cluster sizes. rho=0 returns ``n_units`` unchanged. Repeated measures
    within a cluster are never treated as independent trials.
    """
    n_units = _require_int("n_units", n_units)
    if n_units < 1:
        raise ValueError("n_units must be at least one")
    return n_units / design_effect(icc=icc, cluster_size=cluster_size)


def _scale_required_tasks(
    independent: int | None, icc: float, cluster_size: int
) -> tuple[int | None, float]:
    """Scale an independent task requirement by the cluster design effect.

    Returns ``(clustered_requirement, design_effect)``. A ``None`` independent
    requirement stays ``None`` (nothing is achievable at any n); otherwise the
    requirement is inflated to the next integer of at least two.
    """
    factor = design_effect(icc=icc, cluster_size=cluster_size)
    if independent is None:
        return None, factor
    return max(2, math.ceil(independent * factor)), factor


def clustered_required_tasks_for_effect(
    *,
    baseline: float,
    attempt_effect: float,
    k: int,
    alpha: float = 0.05,
    target_power: float = 0.8,
    pair_correlation: float = 0.0,
    icc: float,
    cluster_size: int,
) -> dict[str, float | int | None]:
    """Clustered (repeated-measure) task requirement for a per-attempt effect.

    Refuses to quote a clustered ``n`` without an explicit ``icc`` declaration.
    Paired covariance (``pair_correlation``, the within-pair term of
    ``_paired_variance``) is kept separate from between-cluster inflation
    (``icc``/``cluster_size`` via ``design_effect``); both are applied. rho=0 keeps
    the requirement identical to the independent plan.
    """
    independent = required_tasks_for_effect(
        baseline=baseline,
        attempt_effect=attempt_effect,
        k=k,
        alpha=alpha,
        target_power=target_power,
        pair_correlation=pair_correlation,
    )
    required, factor = _scale_required_tasks(independent, icc, cluster_size)
    return {
        "k": k,
        "icc": icc,
        "cluster_size": cluster_size,
        "design_effect": factor,
        "required_n_tasks_independent": independent,
        "required_n_tasks_clustered": required,
        "effective_n_tasks": (required / factor) if required is not None else None,
    }


def clustered_minimum_detectable_effect(
    *,
    n_tasks: int,
    k: int,
    baseline: float,
    alpha: float = 0.05,
    target_power: float = 0.8,
    pair_correlation: float = 0.0,
    icc: float,
    cluster_size: int,
) -> dict[str, float | int | None]:
    """Minimum detectable per-attempt difference for ``n_tasks`` clustered observations.

    The independent MDE scales by ``sqrt(design_effect)`` because the required n is
    proportional to variance and the clustered variance is the independent variance
    inflated by ``DE``. Also reports the effective (independent-equivalent) sample
    size. Returns a ``None`` MDE when the effect is not detectable at this n/design.
    """
    factor = design_effect(icc=icc, cluster_size=cluster_size)
    independent = minimum_detectable_effect(
        n_tasks=n_tasks,
        k=k,
        baseline=baseline,
        alpha=alpha,
        target_power=target_power,
        pair_correlation=pair_correlation,
    )
    mde = None
    if independent is not None:
        scaled = independent * math.sqrt(factor)
        mde = scaled if scaled <= 1 - baseline else None
    return {
        "n_tasks": n_tasks,
        "icc": icc,
        "cluster_size": cluster_size,
        "design_effect": factor,
        "effective_n_tasks": n_tasks / factor,
        "minimum_detectable_effect": mde,
    }


def clustered_power_requirements(
    *,
    baseline: float,
    attempt_effect: float,
    max_k: int,
    alpha: float = 0.05,
    target_power: float = 0.8,
    pair_correlation: float = 0.0,
    icc: float,
    cluster_size: int,
) -> list[dict[str, float | int | None]]:
    """Clustered requirement table across ``k`` with design-effect columns.

    Each row quotes the design-effect-inflated clustered requirement alongside the
    independent requirement and the effective (independent-equivalent) sample size,
    so clustered n is never presented as independent trials.
    """
    if max_k < 1:
        raise ValueError("max_k must be positive")
    factor = design_effect(icc=icc, cluster_size=cluster_size)
    rows: list[dict[str, float | int | None]] = []
    for k in range(1, max_k + 1):
        p0 = pass_at_k_probability(baseline, k)
        p1 = pass_at_k_probability(baseline + attempt_effect, k)
        independent = required_tasks_for_effect(
            baseline=baseline,
            attempt_effect=attempt_effect,
            k=k,
            alpha=alpha,
            target_power=target_power,
            pair_correlation=pair_correlation,
        )
        required, _ = _scale_required_tasks(independent, icc, cluster_size)
        rows.append(
            {
                "k": k,
                "baseline_pass_at_k": p0,
                "comparison_pass_at_k": p1,
                "task_level_effect": p1 - p0,
                "icc": icc,
                "cluster_size": cluster_size,
                "design_effect": factor,
                "required_n_tasks_independent": independent,
                "required_n_tasks_clustered": required,
                "effective_n_tasks": (required / factor) if required is not None else None,
                "total_attempts_two_cohorts": 2 * k * required if required else None,
            }
        )
    return rows


def _budget_exhaustion(member: CohortMember) -> bool:
    return member.exception_class in TIMEOUT_BUDGET_EXCEPTION_CLASSES


def _effective_reward(member: CohortMember, *, budget_exhaustion_is_failure: bool) -> float | None:
    if member.exception_class is not None:
        if budget_exhaustion_is_failure and _budget_exhaustion(member):
            return 0.0
        return None
    return float(member.reward) if member.reward is not None else None


def _eligible_task_groups(
    members: list[CohortMember],
    *,
    pairing_key: str,
    budget_exhaustion_is_failure: bool,
) -> tuple[dict[str, list[CohortMember]], int]:
    groups: dict[str, list[CohortMember]] = defaultdict(list)
    missing_pairing_key = 0
    for member in members:
        is_budget_failure = budget_exhaustion_is_failure and _budget_exhaustion(member)
        if (member.exception_class is not None or member.reward is None) and not is_budget_failure:
            continue
        key = _pairing_value(member, pairing_key)
        if key is None:
            missing_pairing_key += 1
            continue
        groups[key].append(member)
    return groups, missing_pairing_key


def _select_first_k_by_started_at(
    attempts: list[CohortMember], k: int
) -> tuple[list[CohortMember] | None, str | None]:
    if len(attempts) < k:
        return None, "fewer than k scored attempts"
    ordered: list[tuple[datetime, CohortMember]] = []
    for item in attempts:
        started = _parse_started_at(item.started_at)
        if started is None:
            return None, "missing or invalid started_at"
        ordered.append((started, item))
    ordered.sort(key=lambda pair: (pair[0], pair[1].trial_id))
    if len(ordered) > k and ordered[k - 1][0] == ordered[k][0]:
        return None, "started_at tie straddles first-k boundary"
    return [item for _, item in ordered[:k]], None


def _task_evidence(
    members: list[CohortMember],
    *,
    pairing_key: str,
    k: int,
    threshold: float,
    budget_exhaustion_is_failure: bool = False,
) -> tuple[dict[str, dict[str, Any]], list[str], int, dict[str, str]]:
    groups, missing_pairing_key = _eligible_task_groups(
        members,
        pairing_key=pairing_key,
        budget_exhaustion_is_failure=budget_exhaustion_is_failure,
    )
    evidence: dict[str, dict[str, Any]] = {}
    insufficient: list[str] = []
    order_unavailable: dict[str, str] = {}
    for key in sorted(groups):
        selected, reason = _select_first_k_by_started_at(groups[key], k)
        if selected is None:
            if reason == "fewer than k scored attempts":
                insufficient.append(key)
            else:
                order_unavailable[key] = reason or "missing or invalid started_at"
            continue
        rewards = [
            reward
            for item in selected
            if (
                reward := _effective_reward(
                    item,
                    budget_exhaustion_is_failure=budget_exhaustion_is_failure,
                )
            )
            is not None
        ]
        passed = [reward >= threshold for reward in rewards]
        evidence[key] = {
            "success": float(any(passed)),
            "all_success": float(all(passed)),
            "mean_reward": statistics.fmean(rewards),
            "members": selected,
        }
    return evidence, insufficient, missing_pairing_key, order_unavailable


def _pass_metric(
    members: list[CohortMember],
    *,
    pairing_key: str,
    k: int,
    threshold: float,
    seed: int,
    metric: str,
    budget_exhaustion_is_failure: bool,
) -> dict[str, Any]:
    evidence, insufficient, missing_pairing_key, order_unavailable = _task_evidence(
        members,
        pairing_key=pairing_key,
        k=k,
        threshold=threshold,
        budget_exhaustion_is_failure=budget_exhaustion_is_failure,
    )
    outcomes = [float(evidence[key][metric]) for key in sorted(evidence)]
    successes = int(sum(outcomes))
    interval = bootstrap_mean_interval(outcomes, seed=seed)
    return {
        "k": k,
        "evidence_unit": "task",
        "selection": "first-k-by-started-at-per-task",
        "passes": successes,
        "n_tasks": len(outcomes),
        "denominator": len(outcomes),
        "rate": statistics.fmean(outcomes) if outcomes else None,
        "bootstrap_95": list(interval) if interval is not None else None,
        "insufficient_attempt_groups": insufficient,
        "unavailable_order_groups": dict(sorted(order_unavailable.items())),
        "missing_pairing_key_trials": missing_pairing_key,
        "selected_trials": {
            key: [item.trial_id for item in evidence[key]["members"]] for key in sorted(evidence)
        },
    }


def _pass_any_first_k(
    members: list[CohortMember],
    *,
    pairing_key: str,
    k: int,
    threshold: float,
    seed: int,
    budget_exhaustion_is_failure: bool = False,
) -> dict[str, Any]:
    return _pass_metric(
        members,
        pairing_key=pairing_key,
        k=k,
        threshold=threshold,
        seed=seed,
        metric="success",
        budget_exhaustion_is_failure=budget_exhaustion_is_failure,
    )


def _pass_all_first_k(
    members: list[CohortMember],
    *,
    pairing_key: str,
    k: int,
    threshold: float,
    seed: int,
    budget_exhaustion_is_failure: bool = False,
) -> dict[str, Any]:
    return _pass_metric(
        members,
        pairing_key=pairing_key,
        k=k,
        threshold=threshold,
        seed=seed,
        metric="all_success",
        budget_exhaustion_is_failure=budget_exhaustion_is_failure,
    )


def _unbiased_metric(
    members: list[CohortMember],
    *,
    pairing_key: str,
    k: int,
    threshold: float,
    seed: int,
    estimator: Callable[[int, int, int], float | None],
    budget_exhaustion_is_failure: bool,
) -> dict[str, Any]:
    groups, missing_pairing_key = _eligible_task_groups(
        members,
        pairing_key=pairing_key,
        budget_exhaustion_is_failure=budget_exhaustion_is_failure,
    )
    estimates: list[float] = []
    task_estimates: dict[str, float] = {}
    insufficient: list[str] = []
    for key in sorted(groups):
        attempts = groups[key]
        n = len(attempts)
        if n < k:
            insufficient.append(key)
            continue
        successes = 0
        for item in attempts:
            reward = _effective_reward(
                item,
                budget_exhaustion_is_failure=budget_exhaustion_is_failure,
            )
            if reward is not None and reward >= threshold:
                successes += 1
        estimate = estimator(n, successes, k)
        if estimate is None:
            insufficient.append(key)
            continue
        task_estimates[key] = estimate
        estimates.append(estimate)
    interval = bootstrap_mean_interval(estimates, seed=seed)
    return {
        "k": k,
        "evidence_unit": "task",
        "selection": "all-eligible-attempts-per-task-unbiased",
        "n_tasks": len(estimates),
        "denominator": len(estimates),
        "rate": statistics.fmean(estimates) if estimates else None,
        "bootstrap_95": list(interval) if interval is not None else None,
        "insufficient_attempt_groups": insufficient,
        "missing_pairing_key_trials": missing_pairing_key,
        "task_estimates": task_estimates,
    }

def _cost_per_solved_task(
    cohort: list[CohortMember],
    spec: CohortComparisonSpec,
) -> dict[str, Any]:
    """Recorded cost per solved task for one arm.

    The numerator sums the recorded execution cost of every selected trial,
    including failed and unscored attempts; the denominator counts distinct
    pairing-key task instances with at least one valid pass, so repeated
    successes cannot inflate it. A missing or invalid per-trial cost is never
    zero-filled: it is counted, and it makes the ratio unavailable. Recorded
    native costs (including an explicit recorded zero for a no-API-charge
    local route) are evidence about recorded execution cost, not invoices.
    """
    recorded: list[float] = []
    missing_or_invalid = 0
    for member in cohort:
        cost = member.cost_usd
        if cost is None or not math.isfinite(cost) or cost < 0:
            missing_or_invalid += 1
        else:
            recorded.append(float(cost))
    solved_tasks: set[str] = set()
    for member in cohort:
        reward = _effective_reward(
            member,
            budget_exhaustion_is_failure=spec.budget_exhaustion_is_failure,
        )
        if reward is None or reward < spec.pass_threshold:
            continue
        key = _pairing_value(member, spec.pairing_key)
        if key is not None:
            solved_tasks.add(key)
    total = sum(recorded)
    reason: str | None = None
    if not solved_tasks:
        reason = "no solved tasks"
    elif missing_or_invalid:
        reason = (
            f"incomplete cost evidence: {missing_or_invalid} of {len(cohort)} "
            "selected trial(s) lack a valid recorded cost"
        )
    return {
        "cost_basis": COST_BASIS_RECORDED_NATIVE,
        "recorded_cost_total_usd": total,
        "cost_trial_count": len(recorded),
        "missing_or_invalid_cost_trial_count": missing_or_invalid,
        "solved_task_count": len(solved_tasks),
        "pairing_key": spec.pairing_key,
        "cost_per_solved_task_usd": (
            total / len(solved_tasks) if reason is None else None
        ),
        "unavailable_reason": reason,
    }


def _summarize_cohort(
    label: str,
    members: list[CohortMember],
    spec: CohortComparisonSpec,
) -> dict[str, Any]:
    cohort = [member for member in members if member.cohort == label]
    exceptions = Counter(
        member.exception_class
        for member in cohort
        if member.exception_class is not None
        and not (spec.budget_exhaustion_is_failure and _budget_exhaustion(member))
    )
    missing_rewards = [
        member.trial_id
        for member in cohort
        if member.exception_class is None and member.reward is None
    ]
    capability = [
        member
        for member in cohort
        if _effective_reward(
            member,
            budget_exhaustion_is_failure=spec.budget_exhaustion_is_failure,
        )
        is not None
    ]
    return {
        "label": label,
        "n_total": len(cohort),
        "capability_denominator": len(capability),
        "exception_count": sum(exceptions.values()),
        "exceptions": dict(sorted(exceptions.items())),
        "missing_reward_count": len(missing_rewards),
        "missing_reward_trials": sorted(missing_rewards),
        "trial_pass_count": sum(
            (
                _effective_reward(
                    member,
                    budget_exhaustion_is_failure=spec.budget_exhaustion_is_failure,
                )
                or 0.0
            )
            >= spec.pass_threshold
            for member in capability
        ),
        "reward": _numeric_summary(
            [
                float(reward)
                for member in capability
                if (
                    reward := _effective_reward(
                        member,
                        budget_exhaustion_is_failure=spec.budget_exhaustion_is_failure,
                    )
                )
                is not None
            ]
        ),
        "duration_seconds": _numeric_summary(
            [
                float(member.duration_seconds)
                for member in capability
                if member.duration_seconds is not None
            ]
        ),
        "input_tokens": _numeric_summary(
            [float(member.input_tokens) for member in capability if member.input_tokens is not None]
        ),
        "output_tokens": _numeric_summary(
            [
                float(member.output_tokens)
                for member in capability
                if member.output_tokens is not None
            ]
        ),
        "cost_usd": _numeric_summary(
            [float(member.cost_usd) for member in capability if member.cost_usd is not None]
        ),
        "cost_per_solved_task": _cost_per_solved_task(cohort, spec),
        "tool_call_count": _numeric_summary(
            [float(member.tool_call_count) for member in capability]
        ),
        "pass_any_first_k": [
            _pass_any_first_k(
                cohort,
                pairing_key=spec.pairing_key,
                k=k,
                threshold=spec.pass_threshold,
                seed=_bootstrap_seed(spec.comparison_id, label, "pass-any-first", k),
                budget_exhaustion_is_failure=spec.budget_exhaustion_is_failure,
            )
            for k in spec.pass_k
        ],
        "pass_all_first_k": [
            _pass_all_first_k(
                cohort,
                pairing_key=spec.pairing_key,
                k=k,
                threshold=spec.pass_threshold,
                seed=_bootstrap_seed(spec.comparison_id, label, "pass-all-first", k),
                budget_exhaustion_is_failure=spec.budget_exhaustion_is_failure,
            )
            for k in spec.pass_k
        ],
        "pass_at_k_unbiased": [
            _unbiased_metric(
                cohort,
                pairing_key=spec.pairing_key,
                k=k,
                threshold=spec.pass_threshold,
                seed=_bootstrap_seed(spec.comparison_id, label, "pass-at-unbiased", k),
                estimator=pass_at_k_unbiased,
                budget_exhaustion_is_failure=spec.budget_exhaustion_is_failure,
            )
            for k in spec.pass_k
        ],
        "pass_power_k_unbiased": [
            _unbiased_metric(
                cohort,
                pairing_key=spec.pairing_key,
                k=k,
                threshold=spec.pass_threshold,
                seed=_bootstrap_seed(spec.comparison_id, label, "pass-power-unbiased", k),
                estimator=pass_power_k_unbiased,
                budget_exhaustion_is_failure=spec.budget_exhaustion_is_failure,
            )
            for k in spec.pass_k
        ],
        "members": [asdict(member) for member in cohort],
    }


def _elicitation_tuple(
    label: str,
    members: list[CohortMember],
    *,
    k: int,
) -> tuple[dict[str, Any] | None, list[str]]:
    if not members:
        return None, [f"cohort {label!r} contributes no paired task attempts"]
    values = {
        (
            member.agent_version,
            member.model_name,
            member.preamble_hash,
            member.toolset_digest,
            json.dumps(member.toolset, sort_keys=True) if member.toolset is not None else None,
        )
        for member in members
    }
    if len(values) != 1:
        return None, [f"cohort {label!r} mixes {len(values)} elicitation tuples"]
    agent_version, model_pin, preamble_hash, toolset_digest, toolset_json = next(iter(values))
    missing = [
        name
        for name, value in (
            ("agent version", agent_version),
            ("model pin", model_pin),
            ("preamble hash", preamble_hash),
            ("toolset", toolset_digest),
        )
        if not value
    ]
    if missing:
        return None, [f"cohort {label!r} is missing {', '.join(missing)}"]
    return {
        "agent_version": agent_version,
        "model_pin": model_pin,
        "preamble_hash": preamble_hash,
        "toolset": json.loads(toolset_json) if toolset_json is not None else None,
        "toolset_digest": toolset_digest,
        "k": k,
    }, []


def _paired_results(
    members: list[CohortMember],
    spec: CohortComparisonSpec,
    warnings: list[str],
) -> list[dict[str, Any]]:
    baseline = spec.cohorts[0].label
    results: list[dict[str, Any]] = []
    for selector in spec.cohorts[1:]:
        for k in spec.pass_k:
            baseline_members = [item for item in members if item.cohort == baseline]
            comparison_members = [item for item in members if item.cohort == selector.label]
            baseline_tasks, baseline_insufficient, baseline_missing, baseline_order = (
                _task_evidence(
                    baseline_members,
                    pairing_key=spec.pairing_key,
                    k=k,
                    threshold=spec.pass_threshold,
                    budget_exhaustion_is_failure=spec.budget_exhaustion_is_failure,
                )
            )
            comparison_tasks, comparison_insufficient, comparison_missing, comparison_order = (
                _task_evidence(
                    comparison_members,
                    pairing_key=spec.pairing_key,
                    k=k,
                    threshold=spec.pass_threshold,
                    budget_exhaustion_is_failure=spec.budget_exhaustion_is_failure,
                )
            )
            paired_keys = sorted(set(baseline_tasks) & set(comparison_tasks))
            pass_deltas = [
                float(comparison_tasks[key]["success"]) - float(baseline_tasks[key]["success"])
                for key in paired_keys
            ]
            pass_all_first_deltas = [
                float(comparison_tasks[key]["all_success"])
                - float(baseline_tasks[key]["all_success"])
                for key in paired_keys
            ]
            reward_deltas = [
                float(comparison_tasks[key]["mean_reward"])
                - float(baseline_tasks[key]["mean_reward"])
                for key in paired_keys
            ]
            interval = bootstrap_mean_interval(
                pass_deltas,
                seed=_bootstrap_seed(spec.comparison_id, baseline, selector.label, k),
            )
            pass_all_first_interval = bootstrap_mean_interval(
                pass_all_first_deltas,
                seed=_bootstrap_seed(spec.comparison_id, baseline, selector.label, "pass-power", k),
            )
            selected_baseline = [
                member for key in paired_keys for member in baseline_tasks[key]["members"]
            ]
            selected_comparison = [
                member for key in paired_keys for member in comparison_tasks[key]["members"]
            ]
            baseline_elicitation, baseline_reasons = _elicitation_tuple(
                baseline, selected_baseline, k=k
            )
            comparison_elicitation, comparison_reasons = _elicitation_tuple(
                selector.label, selected_comparison, k=k
            )
            unpaired = sorted(set(baseline_tasks) ^ set(comparison_tasks))
            reasons: list[str] = []
            if spec.pairing_key not in {"task_block_id", "task_digest", "task_name"}:
                reasons.append(f"pairing key {spec.pairing_key!r} is not a task identity")
            if warnings:
                reasons.extend(warnings)
            if len(paired_keys) < 2:
                reasons.append(f"only {len(paired_keys)} paired task(s); at least 2 are required")
            if unpaired:
                reasons.append(f"{len(unpaired)} eligible task(s) are not paired across cohorts")
            if baseline_insufficient or comparison_insufficient:
                reasons.append(
                    "fewer than k scored attempts for "
                    f"{len(set(baseline_insufficient + comparison_insufficient))} task(s)"
                )
            order_unavailable = dict(baseline_order) | dict(comparison_order)
            if order_unavailable:
                reasons.append(
                    "first-k order is unavailable for "
                    f"{len(set(baseline_order) | set(comparison_order))} task(s)"
                )
                reasons.extend(
                    f"task {key}: {reason}" for key, reason in sorted(order_unavailable.items())
                )
            if baseline_missing or comparison_missing:
                reasons.append(
                    f"{baseline_missing + comparison_missing} scored trial(s) lack task identity"
                )
            reasons.extend(baseline_reasons)
            reasons.extend(comparison_reasons)
            if interval is None:
                reasons.append("the paired task interval is unavailable")
            elif interval[0] <= 0 <= interval[1]:
                reasons.append(
                    f"the paired 95% interval [{interval[0]:.3f}, {interval[1]:.3f}] includes zero"
                )
            reasons = list(dict.fromkeys(reasons))
            if reasons:
                statement = f"{NOT_COMPARABLE}: {'; '.join(reasons)}"
                ranking = None
            elif interval is not None:
                if interval[0] > 0:
                    ranking = f"{selector.label} > {baseline}"
                else:
                    ranking = f"{baseline} > {selector.label}"
                statement = (
                    f"Ranking: {ranking}; n_tasks={len(paired_keys)}, k={k}, "
                    f"paired bootstrap 95% interval=[{interval[0]:.3f}, {interval[1]:.3f}]."
                )
            else:  # Guarded above; retained so static analysis sees total assignment.
                raise AssertionError("interval unexpectedly unavailable")
            results.append(
                {
                    "baseline": baseline,
                    "comparison": selector.label,
                    "pairing_key": spec.pairing_key,
                    "evidence_unit": "task",
                    "n_tasks": len(paired_keys),
                    "n_pairs": len(paired_keys),
                    "k": k,
                    "mean_pass_any_first_k_delta": (
                        statistics.fmean(pass_deltas) if pass_deltas else None
                    ),
                    "bootstrap_95": list(interval) if interval is not None else None,
                    "mean_pass_all_first_k_delta": (
                        statistics.fmean(pass_all_first_deltas) if pass_all_first_deltas else None
                    ),
                    "pass_all_first_k_bootstrap_95": (
                        list(pass_all_first_interval)
                        if pass_all_first_interval is not None
                        else None
                    ),
                    "pass_all_first_k_wins": sum(value > 0 for value in pass_all_first_deltas),
                    "pass_all_first_k_ties": sum(value == 0 for value in pass_all_first_deltas),
                    "pass_all_first_k_losses": sum(value < 0 for value in pass_all_first_deltas),
                    "mean_reward_delta": (
                        statistics.fmean(reward_deltas) if reward_deltas else None
                    ),
                    "wins": sum(value > 0 for value in pass_deltas),
                    "ties": sum(value == 0 for value in pass_deltas),
                    "losses": sum(value < 0 for value in pass_deltas),
                    "elicitation": {
                        baseline: baseline_elicitation,
                        selector.label: comparison_elicitation,
                    },
                    "rankable": ranking is not None,
                    "ranking": ranking,
                    "statement": statement,
                    "refusal_reasons": reasons,
                    "unpaired_tasks": unpaired,
                    "pairs": [
                        {
                            "key": key,
                            "pass_any_first_k_delta": pass_deltas[index],
                            "pass_all_first_k_delta": pass_all_first_deltas[index],
                            "reward_delta": reward_deltas[index],
                        }
                        for index, key in enumerate(paired_keys)
                    ],
                }
            )
    return results


def compare(spec: CohortComparisonSpec, *, repo_root: Path) -> dict[str, Any]:
    members = assemble_members(repo_root, spec)
    warnings = _validate_comparability(spec, members)
    report = {
        "schema_version": 1,
        "comparison_id": spec.comparison_id,
        "experiment_id": spec.experiment_id,
        "spec_digest": digest_json(spec.model_dump(mode="json")),
        "mode": spec.mode,
        "declared_variable": spec.declared_variable,
        "reward_name": spec.reward_name,
        "pass_threshold": spec.pass_threshold,
        "budget_exhaustion_is_failure": spec.budget_exhaustion_is_failure,
        "pairing_key": spec.pairing_key,
        "validity_warnings": warnings,
        "cohorts": [_summarize_cohort(selector.label, members, spec) for selector in spec.cohorts],
        "paired": _paired_results(members, spec, warnings),
    }
    return report


def summarize_job_evidence(
    job: JobRecord,
    *,
    repo_root: Path,
    k: int,
    reward_name: str = "reward",
    pass_threshold: float = 1.0,
) -> dict[str, Any]:
    members = [
        _member(repo_root, job.name, job.name, job, trial, reward_name) for trial in job.trials
    ]
    evidence, insufficient, missing_pairing_key, order_unavailable = _task_evidence(
        members,
        pairing_key="task_digest",
        k=k,
        threshold=pass_threshold,
    )
    selected = [member for key in sorted(evidence) for member in evidence[key]["members"]]
    elicitation, elicitation_reasons = _elicitation_tuple(job.name, selected, k=k)
    metric = _pass_any_first_k(
        members,
        pairing_key="task_digest",
        k=k,
        threshold=pass_threshold,
        seed=_bootstrap_seed(job.id, k),
    )
    return {
        "job_id": job.id,
        "job_name": job.name,
        "n_trials": len(members),
        "n_tasks": metric["n_tasks"],
        "k": k,
        "pass_any_first_k": metric,
        "pass_all_first_k": _pass_all_first_k(
            members,
            pairing_key="task_digest",
            k=k,
            threshold=pass_threshold,
            seed=_bootstrap_seed(job.id, "pass-all-first", k),
        ),
        "pass_at_k_unbiased": _unbiased_metric(
            members,
            pairing_key="task_digest",
            k=k,
            threshold=pass_threshold,
            seed=_bootstrap_seed(job.id, "pass-at-unbiased", k),
            estimator=pass_at_k_unbiased,
            budget_exhaustion_is_failure=False,
        ),
        "pass_power_k_unbiased": _unbiased_metric(
            members,
            pairing_key="task_digest",
            k=k,
            threshold=pass_threshold,
            seed=_bootstrap_seed(job.id, "pass-power-unbiased", k),
            estimator=pass_power_k_unbiased,
            budget_exhaustion_is_failure=False,
        ),
        "elicitation": elicitation,
        "elicitation_reasons": elicitation_reasons,
        "exception_count": sum(member.exception_class is not None for member in members),
        "missing_reward_count": sum(
            member.exception_class is None and member.reward is None for member in members
        ),
        "insufficient_tasks": insufficient,
        "unavailable_order_groups": dict(sorted(order_unavailable.items())),
        "missing_task_identity_trials": missing_pairing_key,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Cohort comparison: {report['comparison_id']}",
        "",
        f"Mode: `{report['mode']}`. Declared variable: `{report['declared_variable']}`. ",
        f"Experiment: `{report['experiment_id']}`. Reward: `{report['reward_name']}`.",
        "",
    ]
    warnings = report["validity_warnings"]
    if warnings:
        lines.extend(["## Validity warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)
        lines.append("")
    lines.extend(
        [
            "## Outcomes",
            "",
            "| cohort | total | capability denominator | exceptions | pass-any-first-k "
            "| cost per solved task |",
            "|---|---:|---:|---:|---|---|",
        ]
    )
    for cohort in report["cohorts"]:
        pass_cells = []
        for metric in cohort["pass_any_first_k"]:
            interval = metric["bootstrap_95"]
            if metric["rate"] is None:
                value = "n/a"
            else:
                value = f"{metric['rate']:.3f} ({metric['passes']}/{metric['n_tasks']} tasks)"
                if interval is not None:
                    value += f" [{interval[0]:.3f}, {interval[1]:.3f}]"
            pass_cells.append(f"@{metric['k']} {value}")
        cost = cohort["cost_per_solved_task"]
        if cost["cost_per_solved_task_usd"] is None:
            cost_cell = f"unavailable ({cost['unavailable_reason']})"
        else:
            cost_cell = (
                f"${cost['cost_per_solved_task_usd']:.4f} "
                f"({cost['solved_task_count']} solved, "
                f"${cost['recorded_cost_total_usd']:.2f} recorded)"
            )
        lines.append(
            f"| {cohort['label']} | {cohort['n_total']} | "
            f"{cohort['capability_denominator']} | {cohort['exception_count']} | "
            f"{'<br>'.join(pass_cells)} | {cost_cell} |"
        )
    lines.extend(
        [
            "",
            "Exceptions are reported beside, and excluded from, the capability denominator.",
            "Cost per solved task divides the recorded cost of every selected "
            "trial, failed and unscored attempts included, by the count of "
            "distinct solved task instances; it is withheld, never zero-filled, "
            "when costs are incomplete or nothing was solved.",
            "",
        ]
    )
    lines.extend(["## Paired by task", ""])
    for paired in report["paired"]:
        lines.append(
            f"### pass-any-first-k@{paired['k']}: {paired['comparison']} vs {paired['baseline']}"
        )
        lines.append("")
        lines.append(paired["statement"])
        lines.append("")
        lines.append(
            f"Paired task delta={paired['mean_pass_any_first_k_delta']}; "
            f"wins/ties/losses={paired['wins']}/{paired['ties']}/{paired['losses']}."
        )
        lines.append("")
        lines.extend(["Elicitation tuples:", ""])
        for label, elicitation in paired["elicitation"].items():
            value = json.dumps(elicitation, sort_keys=True) if elicitation else "unavailable"
            lines.append(f"- `{label}`: {value}")
        lines.append("")
    lines.extend(
        [
            "",
            "## Interpretation boundary",
            "",
            "Attempts within one task are clustered into one evidence unit. Every interval above ",
            "resamples tasks, and every two-cohort decision uses task-paired deltas. A ranking is ",
            "printed only when the paired interval excludes zero and both elicitation tuples are ",
            "complete; otherwise the report states the refusal reason.",
            "",
        ]
    )
    return "\n".join(lines)


def write_comparison(
    spec_path: Path,
    *,
    repo_root: Path,
    output_root: Path | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    spec = load_spec(spec_path)
    report = compare(spec, repo_root=repo_root)
    destination = (output_root or repo_root / "derived/comparisons").resolve()
    destination.mkdir(parents=True, exist_ok=True)
    json_path = destination / f"{spec.comparison_id}.json"
    markdown_path = destination / f"{spec.comparison_id}.md"
    json_payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    markdown_payload = render_markdown(report)
    for path, payload in ((json_path, json_payload), (markdown_path, markdown_payload)):
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(payload)
        temporary.replace(path)
    return json_path, markdown_path, report


def index_comparison_associations(
    database_url: str,
    *,
    spec_path: Path,
    report: dict[str, Any],
    repo_root: Path,
) -> None:
    """Associate legacy/raw jobs only when a reviewed cohort spec declares it."""
    spec = load_spec(spec_path)
    if report.get("spec_digest") != digest_json(spec.model_dump(mode="json")):
        raise ValueError("comparison report does not match the supplied spec")
    provenance = {
        "comparison_spec": (
            spec_path.resolve().relative_to(repo_root.resolve()).as_posix()
            if repo_root.resolve() in spec_path.resolve().parents
            else spec_path.resolve().as_posix()
        ),
        "spec_digest": report["spec_digest"],
        "declared_variable": spec.declared_variable,
        "mode": spec.mode,
    }
    job_ids = sorted(
        {member["job_id"] for cohort in report["cohorts"] for member in cohort["members"]}
    )
    with psycopg.connect(database_url) as connection:
        connection.execute(
            """
            INSERT INTO experiments (id, source_kind, raw_provenance)
            VALUES (%s, 'cohort-spec', %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (spec.experiment_id, Jsonb(provenance)),
        )
        for job_id in job_ids:
            row = connection.execute(
                "SELECT experiment_id FROM jobs WHERE id = %s", (job_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"comparison job is not indexed: {job_id}")
            if row[0] not in (None, spec.experiment_id):
                raise ValueError(f"job {job_id} is already associated with experiment {row[0]!r}")
            connection.execute(
                "UPDATE jobs SET experiment_id = %s WHERE id = %s",
                (spec.experiment_id, job_id),
            )
