"""Terminus-2 teacher trials to tinker-cookbook chat conversations (HAR-81).

Exports retained Eval Lab Terminus-2 job directories to
``conversations.jsonl`` in the tinker-cookbook ``chat_sl`` shape
(``{"messages": [{"role", "content"}, ...]}``) for student SFT, plus a
manifest binding every conversation to its source trial, reward, teacher
model, and sealed split.

What the model saw (Harbor 0.21.0 ``Terminus2``):

* The chat has no separate system message; the first user turn embeds the
  instruction, terminal state, and skills. ATIF ``user`` steps map to
  ``user`` messages verbatim.
* Each ``agent`` step is one assistant response as emitted (raw-content
  trajectory mode); its ``observation`` string is the next ``user`` turn.
  The last agent step's observation was never sent back (task-complete
  confirmation or run end) and is dropped — the same convention as
  upstream ``traces_utils`` episode export.
* On context summarization with ``linear_history``, the pre-summarization
  segment is sealed as ``trajectory.json`` and each continuation lands in
  ``trajectory.cont-N.json`` holding the rewound chat the continuing model
  saw (``is_copied_context`` steps) plus fresh steps. Each continuation
  segment becomes its own conversation, flagged with its continuation
  index. A continuation whose steps equal an already exported segment's
  (a summarization that failed without splitting the chat) is skipped and
  counted, so the same turns are not trained twice. Summarization
  *subagent* files (``trajectory.summarization-*``) are teacher-internal
  context management, never exported, only counted.
* Harbor's stand-in reply when its model call fails ("Technical
  difficulties. Please continue with the task.") is recorded as an agent
  step but is not model output. Every assistant turn is a training target,
  so a segment ends before its first stand-in; the dropped agent steps are
  counted per conversation.
* Trajectories recorded with parsed ``tool_calls`` (raw-content mode off)
  have lost the model's raw emission; such trials are excluded, not
  reconstructed — reconstruction would fabricate training targets.

Selection: agent is Terminus-2, a verifier reward is present and at or
above the threshold (default 1.0), and the trial's task id is a ``train``
task of the sealed split. A graded trial counts whatever ended its agent
phase (an agent timeout or a ceiling stop included; HAR-81's scored-outcome
rule), and its exception type is recorded. Trials without a reward are
excluded and counted by reason. Any trial whose task id is in the split's
held-out set
REFUSES the whole export: held-out contamination is a protocol violation,
not a filterable row. Teacher ``reasoning_content`` is dropped by default
(``--keep-reasoning`` keeps it as a ``<think>`` prefix). Reward/verifier
metadata never enters the JSONL: rows carry only ``messages`` with
``role``/``content`` keys.

Run with ``python -m evallab.sft_terminus export --root LABEL=PATH ...
--split-manifest split.json --out DIR``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from evallab.evidence_store import evidence_tree_digest
from evallab.interpretation.trajectory_hydration import _DEFAULT_SECRET_PATTERNS
from evallab.mimo_tool_calls import HARBOR_FALLBACK_RESPONSE
from evallab.sft_glm import REWARD_METADATA_KEYS
from evallab.sft_records import (
    SourceRoot,
    TrialSource,
    _parse_root,
    _sha256_file,
    _text_of,
    discover_trial_dirs,
    resolve_outcome,
)
from evallab.sft_split import load_split
from evallab.sft_split import split_digest as split_manifest_digest_of
from evallab.tracing import REDACTION_MARKER_RE, TraceError

CONTRACT_VERSION = "evallab.sft_terminus/1"
CONVERSATIONS_FILE = "conversations.jsonl"
MANIFEST_FILE = "manifest.json"
DEFAULT_REWARD_THRESHOLD = 1.0
#: ``AgentName.TERMINUS_2.value`` plus spellings seen in older configs.
TERMINUS_AGENT_NAMES = frozenset({"terminus-2", "terminus2"})
_CONTINUATION_RE = re.compile(r"^trajectory\.cont-(\d+)\.json$")
_SUMMARIZATION_RE = re.compile(r"^trajectory\.summarization-")

#: Roles the chat_sl renderer accepts; observations become ``user`` turns.
ALLOWED_ROLES = frozenset({"system", "user", "assistant"})


def _normalize_agent_name(name: Any) -> str:
    if not isinstance(name, str):
        return ""
    return name.strip().lower().replace("_", "-")


def _is_terminus(*names: Any) -> bool:
    return any(_normalize_agent_name(name) in TERMINUS_AGENT_NAMES for name in names)


@dataclass(frozen=True)
class SegmentFile:
    """One retained trajectory segment of a Terminus-2 trial."""

    name: str
    segment: Literal["main", "continuation"]
    continuation_index: int
    path: Path
    sha256: str


@dataclass
class SegmentConversion:
    messages: list[dict[str, Any]]
    parsed_tool_calls: bool
    multimodal: bool
    copied_context_messages: int
    system_marker_steps: int
    reasoning_messages: int
    ignored_step_sources: list[str]
    #: Agent steps dropped from the segment's first Harbor stand-in reply on.
    fallback_truncated_agent_steps: int = 0

    @property
    def agent_messages(self) -> int:
        return sum(1 for message in self.messages if message["role"] == "assistant")


@dataclass
class SegmentConversation:
    """One exported conversation (one trajectory segment)."""

    conversation_id: str
    root: str
    trial: str
    job: str
    task_id: str
    model_name: str | None
    reward: float
    session_id: str | None
    segment: Literal["main", "continuation"]
    continuation_index: int
    trajectory_name: str
    trajectory_sha256: str
    conversion: SegmentConversion

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self.conversion.messages

    @property
    def character_count(self) -> int:
        return sum(len(message["content"]) for message in self.messages)

    def to_json_row(self) -> dict[str, Any]:
        return {"messages": [dict(message) for message in self.messages]}

    def to_manifest_entry(self) -> dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "root": self.root,
            "trial": self.trial,
            "job": self.job,
            "task_id": self.task_id,
            "model": self.model_name,
            "reward": self.reward,
            "session_id": self.session_id,
            "segment": self.segment,
            "continuation_index": self.continuation_index,
            "trajectory_name": self.trajectory_name,
            "trajectory_sha256": self.trajectory_sha256,
            "message_count": len(self.messages),
            "character_count": self.character_count,
            "copied_context_messages": self.conversion.copied_context_messages,
            "fallback_truncated_agent_steps": self.conversion.fallback_truncated_agent_steps,
        }


@dataclass
class TrialDisposition:
    root: str
    trial: str
    job: str
    identity_key: str
    task_id: str | None
    reward: float | None
    disposition: Literal["selected", "excluded", "duplicate"]
    reasons: list[str] = field(default_factory=list)
    conversations: list[SegmentConversation] = field(default_factory=list)
    duplicate_of: str | None = None
    #: The sealed task version the trial was matched to (None until matched).
    task_version_digest: str | None = None
    #: What ended the agent phase of a graded trial (None: the agent finished).
    exception_type: str | None = None
    #: Segments not exported, by file name: ``duplicate_of:<segment>`` when the
    #: steps repeat an exported segment's, ``harbor_fallback_only`` when the
    #: segment's first agent step is Harbor's stand-in reply.
    skipped_segments: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "root": self.root,
            "trial": self.trial,
            "job": self.job,
            "identity_key": self.identity_key,
            "task_id": self.task_id,
            "reward": self.reward,
            "disposition": self.disposition,
            "reasons": self.reasons,
            "conversations": [c.conversation_id for c in self.conversations],
        }
        if self.duplicate_of is not None:
            out["duplicate_of"] = self.duplicate_of
        if self.task_version_digest is not None:
            out["task_version_digest"] = self.task_version_digest
        if self.exception_type is not None:
            out["exception_type"] = self.exception_type
        if self.skipped_segments:
            out["skipped_segments"] = dict(sorted(self.skipped_segments.items()))
        return out


@dataclass
class TerminusExportResult:
    dispositions: list[TrialDisposition]
    conversations: list[SegmentConversation]
    exclusion_counts: dict[str, int]
    summarization_subagent_files: int
    duplicates: dict[str, list[str]]
    split_manifest_digest: str
    harness_trees: dict[str, dict[str, Any] | None] = field(default_factory=dict)

    @property
    def selected_trials(self) -> int:
        return sum(1 for item in self.dispositions if item.disposition == "selected")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def discover_segments(trial_dir: Path) -> tuple[list[SegmentFile], int, int]:
    """(segments, summarization subagent files, unrecognized trajectory files)."""
    agent_dir = trial_dir / "agent"
    segments: list[SegmentFile] = []
    subagent = 0
    unrecognized = 0
    for entry in sorted(agent_dir.iterdir()) if agent_dir.is_dir() else []:
        if not entry.is_file():
            continue
        main = entry.name == "trajectory.json"
        continuation = _CONTINUATION_RE.match(entry.name)
        if main or continuation:
            index = int(continuation.group(1)) if continuation else 0
            segments.append(
                SegmentFile(
                    name=entry.name,
                    segment="main" if main else "continuation",
                    continuation_index=index,
                    path=entry,
                    sha256=_sha256_file(entry),
                )
            )
        elif entry.name.startswith("trajectory") and entry.name.endswith(".json"):
            if _SUMMARIZATION_RE.match(entry.name):
                subagent += 1
            else:
                unrecognized += 1
    segments.sort(key=lambda item: (item.segment != "main", item.continuation_index))
    return segments, subagent, unrecognized


def load_trial(root: SourceRoot, trial_dir: Path) -> TrialSource:
    result = _read_json(trial_dir / "result.json")
    result = result if isinstance(result, dict) else {}
    return TrialSource(
        root=root,
        trial_dir=trial_dir,
        result=result,
        trajectory=None,
        trajectory_sha256=None,
        raw_mini=None,
        raw_mini_sha256=None,
    )


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def convert_segment(
    steps: list[dict[str, Any]], *, keep_reasoning: bool
) -> SegmentConversion:
    """ATIF steps of one segment to the chat messages the model saw, in order."""
    items: list[Any] = []  # single message dicts and agent blocks [assistant, obs?]
    agent_block_positions: list[int] = []
    parsed_tool_calls = False
    multimodal = False
    copied = 0
    system_markers = 0
    reasoning_messages = 0
    ignored: list[str] = []
    fallback_truncated = 0
    for position, step in enumerate(steps):
        if not isinstance(step, dict):
            ignored.append("malformed_step")
            continue
        source = step.get("source")
        if source in ("user", "system"):
            text, mm = _text_of(step.get("message"))
            if mm:
                multimodal = True
                continue
            if source == "system":
                extra = step.get("extra")
                if (isinstance(extra, dict) and extra.get("context_management")) or not text:
                    # Context-management marker step, never model-visible.
                    system_markers += 1
                    continue
            items.append({"role": source, "content": text or ""})
        elif source == "agent":
            if step.get("tool_calls"):
                # Parsed mode: the raw model emission was not retained.
                parsed_tool_calls = True
            text, mm = _text_of(step.get("message"))
            if mm:
                multimodal = True
            if (text or "").strip() == HARBOR_FALLBACK_RESPONSE:
                # Harbor's stand-in for a failed model call, not model output.
                fallback_truncated = sum(
                    1
                    for later in steps[position:]
                    if isinstance(later, dict) and later.get("source") == "agent"
                )
                break
            content = text or ""
            reasoning = step.get("reasoning_content")
            if keep_reasoning and isinstance(reasoning, str) and reasoning:
                content = f"<think>\n{reasoning}\n</think>\n\n{content}"
                reasoning_messages += 1
            block: list[dict[str, Any]] = [{"role": "assistant", "content": content}]
            observation = step.get("observation")
            results = observation.get("results") if isinstance(observation, dict) else None
            observation_texts: list[str] = []
            for result in results or []:
                if not isinstance(result, dict):
                    continue
                obs_text, obs_mm = _text_of(result.get("content"))
                if obs_mm:
                    multimodal = True
                    continue
                if obs_text:
                    observation_texts.append(obs_text)
            if observation_texts:
                # Terminal observations as user turns, exactly as fed back.
                block.append({"role": "user", "content": "\n".join(observation_texts)})
            agent_block_positions.append(len(items))
            items.append(block)
        else:
            ignored.append(str(source))
        if step.get("is_copied_context"):
            copied += 1
    if agent_block_positions:
        # The final agent step's observation was never sent back to the model.
        last = agent_block_positions[-1]
        items[last] = items[last][:1]
    messages: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, list):
            messages.extend(item)
        else:
            messages.append(item)
    return SegmentConversion(
        messages=messages,
        parsed_tool_calls=parsed_tool_calls,
        multimodal=multimodal,
        copied_context_messages=copied,
        system_marker_steps=system_markers,
        reasoning_messages=reasoning_messages,
        ignored_step_sources=ignored,
        fallback_truncated_agent_steps=fallback_truncated,
    )


def _validate_conversation(messages: list[dict[str, Any]]) -> None:
    """The student-visible surface: role/content only, no reward metadata."""
    for message in messages:
        if set(message) - {"role", "content"}:
            raise TraceError(
                f"conversation message carries foreign keys: {sorted(set(message))}"
            )
        if message["role"] not in ALLOWED_ROLES:
            raise TraceError(f"conversation message has unsupported role: {message['role']!r}")
        if not isinstance(message["content"], str):
            raise TraceError("conversation message content must be a string")
    leaked = sorted(set(REWARD_METADATA_KEYS) & {k for m in messages for k in m})
    if leaked:
        raise TraceError(f"reward metadata reached conversation messages: {leaked}")


def _scan_student_text(messages: Iterable[dict[str, Any]]) -> list[str]:
    """Redaction markers and secret-shaped strings in student-visible text."""
    findings: list[str] = []
    for message in messages:
        text = message["content"]
        if text and REDACTION_MARKER_RE.search(text):
            findings.append("redacted_model_visible_context")
        for pattern in _DEFAULT_SECRET_PATTERNS:
            if text and pattern.search(text):
                findings.append("secret_pattern_in_context")
                break
    return findings


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _task_id_of(result: dict[str, Any]) -> str | None:
    task_name = result.get("task_name")
    if isinstance(task_name, str) and task_name.strip():
        return task_name.rsplit("/", 1)[-1]
    return None


def _harness_tree_binding(job_dir: Path) -> dict[str, Any] | None:
    metadata = _read_json(job_dir / "lab-metadata.json")
    recorded = metadata.get("harness_tree") if isinstance(metadata, dict) else None
    recorded_sha = recorded.get("sha256") if isinstance(recorded, dict) else None
    retained = job_dir / "harness-tree"
    if retained.is_dir():
        try:
            actual = evidence_tree_digest(retained)
        except (OSError, ValueError):
            actual = None
        if actual is not None:
            return {"sha256": actual, "verified": actual == recorded_sha}
    if isinstance(recorded_sha, str):
        return {"sha256": recorded_sha, "verified": False}
    return None


def _teacher_model(trajectory: dict[str, Any], result: dict[str, Any]) -> str | None:
    agent = trajectory.get("agent")
    name = agent.get("model_name") if isinstance(agent, dict) else None
    if isinstance(name, str) and name:
        return name
    config_agent = result.get("config")
    config_agent = config_agent.get("agent") if isinstance(config_agent, dict) else None
    name = config_agent.get("model_name") if isinstance(config_agent, dict) else None
    return name if isinstance(name, str) and name else None


def _identity_key(trial: TrialSource, main_sha: str | None) -> str:
    session = trial.session_id
    if session:
        return f"session:{session}"
    if main_sha:
        return f"sha256:{main_sha}"
    return f"path:{trial.root.label}:{trial.relative_path}"


def _split_task_entries(
    split_manifest: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Index sealed per-task entries by full task name and by task id."""
    by_name: dict[str, dict[str, Any]] = {}
    by_id: dict[str, dict[str, Any]] = {}
    for entry in split_manifest.get("tasks") or ():
        if not isinstance(entry, dict):
            continue
        name = entry.get("task_name")
        task_id = entry.get("task_id")
        if isinstance(name, str):
            by_name[name] = entry
        if isinstance(task_id, str):
            by_id[task_id] = entry
    return by_name, by_id


def default_task_store_root() -> Path | None:
    """Shared snapshot store (primary checkout), when it exists locally."""
    from evallab.task_catalog import default_task_store_root as catalog_store_root

    root = catalog_store_root(Path(__file__).resolve().parents[2])
    return root if root.is_dir() else None


def snapshot_task_dir(
    sources: dict[str, Any], entry: dict[str, Any], task_store_root: Path
) -> Path | None:
    """Locate a sealed task's package directory in the shared snapshot store."""
    from evallab.task_catalog import hf_task_store, snapshot_dir_name

    pinned = sources.get(entry.get("domain"))
    if not isinstance(pinned, str):
        return None
    repo, _, revision = pinned.partition("@")
    org, _, repo_name = repo.partition("/")
    task_id = entry.get("task_id")
    if not org or not repo_name or not revision or not isinstance(task_id, str):
        return None
    return (
        hf_task_store(task_store_root)
        / snapshot_dir_name(org, repo_name, revision)
        / "tasks"
        / task_id
    )


def snapshot_task_version_digest(
    sources: dict[str, Any], entry: dict[str, Any], task_store_root: Path
) -> str | None:
    """The on-disk task dir digest (None when the store dir is unavailable).

    Uses the catalog's digest scheme (:func:`evallab.registry
    .task_directory_digest`); a recomputed digest that differs from the
    sealed ``task_version_digest`` means the snapshot drifted and the trial
    matched only by name.
    """
    from evallab.registry import task_directory_digest

    task_dir = snapshot_task_dir(sources, entry, task_store_root)
    if task_dir is None or not task_dir.is_dir():
        return None
    try:
        return task_directory_digest(task_dir)
    except (OSError, ValueError):
        return None


def export_conversations(
    roots: list[SourceRoot],
    *,
    split_manifest_path: Path,
    reward_threshold: float = DEFAULT_REWARD_THRESHOLD,
    keep_reasoning: bool = False,
    task_store_root: Path | None = None,
) -> TerminusExportResult:
    """Select and convert trials. Raises TraceError on held-out contamination.

    A trial resolves to a sealed task by full task name first, then task id.
    When the pinned snapshot task directory is available under
    ``task_store_root`` (explicit, else the shared store when it exists),
    the trial's task dir digest must equal the sealed ``task_version_digest``;
    a recomputed digest that differs excludes the trial as
    ``task_version_drift``. Without the store, matching is by name/id alone.
    """
    split_manifest = load_split(split_manifest_path)
    digest = split_manifest_digest_of(split_manifest)
    entries_by_name, entries_by_id = _split_task_entries(split_manifest)
    store_root = task_store_root if task_store_root is not None else default_task_store_root()
    sources = split_manifest.get("sources") or {}

    loaded: list[tuple[SourceRoot, Path, TrialSource]] = []
    for root in roots:
        for trial_dir in discover_trial_dirs(root.path):
            loaded.append((root, trial_dir, load_trial(root, trial_dir)))

    # Dedupe identical teacher sessions across roots (first occurrence wins).
    by_identity: dict[str, tuple[SourceRoot, Path, TrialSource]] = {}
    order: list[str] = []
    duplicates: dict[str, list[str]] = {}
    for root, trial_dir, trial in loaded:
        segments, _, _ = discover_segments(trial_dir)
        main_sha = next((s.sha256 for s in segments if s.segment == "main"), None)
        key = _identity_key(trial, main_sha)
        if key in by_identity:
            duplicates.setdefault(key, []).append(f"{root.label}:{trial.relative_path}")
            continue
        by_identity[key] = (root, trial_dir, trial)
        order.append(key)

    dispositions: list[TrialDisposition] = []
    conversations: list[SegmentConversation] = []
    exclusion_counts: dict[str, int] = {}
    subagent_files = 0
    refusals: list[dict[str, str]] = []
    harness_trees: dict[str, dict[str, Any] | None] = {}

    def _exclude(disposition: TrialDisposition, reason: str) -> None:
        disposition.disposition = "excluded"
        disposition.reasons = sorted(set(disposition.reasons + [reason]))
        exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1

    for key in order:
        root, trial_dir, trial = by_identity[key]
        segments, subagent, _unrecognized = discover_segments(trial_dir)
        subagent_files += subagent
        outcome = resolve_outcome(trial)
        task_id = _task_id_of(trial.result)
        job = trial_dir.parent.name
        disposition = TrialDisposition(
            root=root.label,
            trial=trial.relative_path,
            job=job,
            identity_key=key,
            task_id=task_id,
            reward=outcome.reward,
            disposition="selected",
        )
        if job not in harness_trees:
            harness_trees[job] = _harness_tree_binding(trial_dir.parent)
        dispositions.append(disposition)

        trajectory = None
        if segments:
            payload = _read_json(segments[0].path)
            trajectory = payload if isinstance(payload, dict) else None
        agent_names = [
            (trajectory or {}).get("agent", {}).get("name")
            if isinstance((trajectory or {}).get("agent"), dict)
            else None,
            (trial.result.get("config") or {}).get("agent", {}).get("name")
            if isinstance(trial.result.get("config"), dict)
            and isinstance(trial.result.get("config", {}).get("agent"), dict)
            else None,
        ]
        if not _is_terminus(*agent_names):
            _exclude(disposition, "not_terminus_2")
            continue
        if outcome.reward is None:
            reason = (
                f"exception:{outcome.exception_type}"
                if outcome.exception_type
                else "verifier_incomplete"
            )
            _exclude(disposition, reason)
            continue
        disposition.exception_type = outcome.exception_type
        if outcome.reward < reward_threshold:
            _exclude(disposition, "reward_below_threshold")
            continue
        reward = outcome.reward
        if task_id is None:
            _exclude(disposition, "no_task_id")
            continue
        task_name = trial.result.get("task_name")
        entry = entries_by_name.get(task_name) if isinstance(task_name, str) else None
        if entry is None:
            entry = entries_by_id.get(task_id)
        if entry is None:
            _exclude(disposition, "task_not_in_split")
            continue
        task_id = entry["task_id"]
        disposition.task_id = task_id
        disposition.task_version_digest = entry["task_version_digest"]
        if store_root is not None:
            task_dir_digest = snapshot_task_version_digest(sources, entry, store_root)
            if task_dir_digest is not None and task_dir_digest != entry["task_version_digest"]:
                _exclude(disposition, "task_version_drift")
                continue
        if entry["split"] == "heldout":
            refusals.append(
                {
                    "root": root.label,
                    "trial": trial.relative_path,
                    "task_id": task_id,
                    "task_version_digest": entry["task_version_digest"],
                }
            )
            continue
        if not segments or not any(segment.segment == "main" for segment in segments):
            _exclude(disposition, "no_trajectory")
            continue

        trial_conversations: list[SegmentConversation] = []
        trial_reasons: list[str] = []
        exported_steps: dict[str, str] = {}
        for segment in segments:
            payload = _read_json(segment.path)
            if not isinstance(payload, dict):
                trial_reasons.append("unparseable_trajectory_segment")
                continue
            steps = payload.get("steps")
            if not isinstance(steps, list):
                trial_reasons.append("unparseable_trajectory_segment")
                continue
            fingerprint = hashlib.sha256(json.dumps(steps, sort_keys=True).encode()).hexdigest()
            if fingerprint in exported_steps:
                # Terminus2._summarize counts a summarization before it can fail,
                # and a failed proactive one is swallowed: the chat is not split,
                # yet the final dump takes the next cont-N name. Same turns, twice.
                disposition.skipped_segments[segment.name] = (
                    f"duplicate_of:{exported_steps[fingerprint]}"
                )
                continue
            exported_steps[fingerprint] = segment.name
            conversion = convert_segment(steps, keep_reasoning=keep_reasoning)
            if conversion.parsed_tool_calls:
                trial_reasons.append("parsed_steps_not_raw_content")
                continue
            if conversion.multimodal:
                trial_reasons.append("unsupported_multimodal_content")
                continue
            if conversion.agent_messages == 0 and conversion.fallback_truncated_agent_steps:
                disposition.skipped_segments[segment.name] = "harbor_fallback_only"
                continue
            if conversion.agent_messages == 0:
                trial_reasons.append("no_agent_steps")
                continue
            trial_reasons.extend(_scan_student_text(conversion.messages))
            _validate_conversation(conversion.messages)
            session_id = payload.get("session_id")
            trial_conversations.append(
                SegmentConversation(
                    conversation_id=hashlib.sha256(
                        f"{key}|{segment.name}|{segment.sha256}".encode()
                    ).hexdigest()[:24],
                    root=root.label,
                    trial=trial.relative_path,
                    job=job,
                    task_id=task_id,
                    model_name=_teacher_model(payload, trial.result),
                    reward=reward,
                    session_id=session_id if isinstance(session_id, str) else None,
                    segment=segment.segment,
                    continuation_index=segment.continuation_index,
                    trajectory_name=segment.name,
                    trajectory_sha256=segment.sha256,
                    conversion=conversion,
                )
            )
        if trial_reasons:
            for reason in sorted(set(trial_reasons)):
                _exclude(disposition, reason)
            continue
        disposition.conversations = trial_conversations
        conversations.extend(trial_conversations)

    for key, paths in duplicates.items():
        winner_root, winner_dir, _winner_trial = by_identity[key]
        duplicate_of = f"{winner_root.label}:{winner_dir.relative_to(winner_root.path).as_posix()}"
        for path in paths:
            label, _, relative = path.partition(":")
            dispositions.append(
                TrialDisposition(
                    root=label,
                    trial=relative,
                    job=winner_dir.parent.name,
                    identity_key=key,
                    task_id=None,
                    reward=None,
                    disposition="duplicate",
                    reasons=["same_source_identity"],
                    duplicate_of=duplicate_of,
                )
            )

    if refusals:
        listing = "; ".join(f"{r['root']}:{r['trial']} ({r['task_id']})" for r in refusals)
        raise TraceError(
            "refusing export: held-out task(s) appeared in teacher roots: "
            f"{listing}. Held-out tasks must never produce training data."
        )

    dispositions.sort(key=lambda item: (item.root, item.trial))
    return TerminusExportResult(
        dispositions=dispositions,
        conversations=conversations,
        exclusion_counts=dict(sorted(exclusion_counts.items())),
        summarization_subagent_files=subagent_files,
        duplicates=dict(sorted(duplicates.items())),
        split_manifest_digest=digest,
        harness_trees=harness_trees,
    )


def write_export(
    result: TerminusExportResult,
    out_dir: Path,
    *,
    roots: list[SourceRoot],
    split_manifest_path: Path,
    reward_threshold: float,
    keep_reasoning: bool,
) -> dict[str, Any]:
    """Write ``conversations.jsonl`` and the manifest; refuses non-empty dirs."""
    if out_dir.exists() and any(out_dir.iterdir()):
        raise TraceError(f"output directory is not empty: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    conversations_path = out_dir / CONVERSATIONS_FILE
    with conversations_path.open("w", encoding="utf-8") as handle:
        for conversation in result.conversations:
            handle.write(
                json.dumps(conversation.to_json_row(), ensure_ascii=False, sort_keys=True) + "\n"
            )

    models = sorted(
        {c.model_name for c in result.conversations if c.model_name is not None}
    )
    segments_main = sum(1 for c in result.conversations if c.segment == "main")
    segments_continuation = len(result.conversations) - segments_main
    manifest = {
        "contract": CONTRACT_VERSION,
        "counts": {
            "conversations": len(result.conversations),
            "trials_selected": result.selected_trials,
            "trials_excluded": sum(
                1 for d in result.dispositions if d.disposition == "excluded"
            ),
            "duplicates": sum(1 for d in result.dispositions if d.disposition == "duplicate"),
            "segments_main": segments_main,
            "segments_continuation": segments_continuation,
        },
        "exclusion_counts": result.exclusion_counts,
        "summarization_subagent_files": result.summarization_subagent_files,
        "teacher_model": models[0] if len(models) == 1 else None,
        "teacher_models_distinct": models,
        "reward_threshold": reward_threshold,
        "reasoning_policy": "kept_as_think_prefix" if keep_reasoning else "dropped",
        "split_manifest": {
            "path": split_manifest_path.as_posix(),
            "manifest_digest": result.split_manifest_digest,
        },
        "harness_tree_digests": dict(sorted(result.harness_trees.items())),
        "roots": [{"label": root.label, "path": root.path.as_posix()} for root in roots],
        "conversations": [c.to_manifest_entry() for c in result.conversations],
        "duplicates": result.duplicates,
        "trials": [d.to_json() for d in result.dispositions],
    }
    manifest["conversations_sha256"] = _sha256_file(conversations_path)
    (out_dir / MANIFEST_FILE).write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="export Terminus-2 trials to chat_sl conversations")
    export.add_argument(
        "--root",
        action="append",
        required=True,
        type=_parse_root,
        help="LABEL=PATH job/trial root; repeatable",
    )
    export.add_argument(
        "--split-manifest",
        type=Path,
        required=True,
        help="sealed split manifest from evallab.sft_split (required; held-out tasks refuse)",
    )
    export.add_argument("--out", type=Path, required=True, help="new, empty output directory")
    export.add_argument(
        "--reward-threshold",
        type=float,
        default=DEFAULT_REWARD_THRESHOLD,
        help="minimum verifier reward to select a trial (default 1.0)",
    )
    export.add_argument(
        "--keep-reasoning",
        action="store_true",
        help="keep teacher reasoning_content as a <think> prefix (default: dropped)",
    )
    export.add_argument(
        "--task-store-root",
        type=Path,
        default=None,
        help="snapshot store root enabling task_version_digest verification "
        "(default: shared store when it exists, else name/id matching only)",
    )
    args = parser.parse_args(argv)
    try:
        result = export_conversations(
            args.root,
            split_manifest_path=args.split_manifest,
            reward_threshold=args.reward_threshold,
            keep_reasoning=args.keep_reasoning,
            task_store_root=args.task_store_root,
        )
        manifest = write_export(
            result,
            args.out,
            roots=args.root,
            split_manifest_path=args.split_manifest,
            reward_threshold=args.reward_threshold,
            keep_reasoning=args.keep_reasoning,
        )
    except TraceError as exc:
        print(f"error: {exc}")
        return 2
    print(
        json.dumps(
            {
                "out": args.out.as_posix(),
                "counts": manifest["counts"],
                "exclusion_counts": manifest["exclusion_counts"],
                "conversations_sha256": manifest["conversations_sha256"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
