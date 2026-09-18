"""Faithful SFT training records from retained Harbor trials.

The record is the boundary between the Harness lane (capture/export fidelity)
and the SFT lane (tokenizer, template, loss mask, trainer). It carries the
model-visible history of one real trial as *model-neutral* messages plus
explicit supervision targets and provenance. It never renders a chat template
and never decides token-level masks; those belong to the consumer.

Why upstream ``harbor.utils.traces_utils.export_traces`` is not the record
source (0.21.0, ``_extract_single_episode_conversation``): it maps ``system``
steps to the ``user`` role, serialises tool calls into Hermes-style
``<tool_call>`` text the model never emitted, flattens every observation into a
single ``user`` string (dropping ``source_call_id`` linkage), and injects
``reasoning_content`` as supervised ``<think>`` text. Each of those is a fidelity
loss for training. The HF-dataset script built on it (``export_harbor_traces``
on the data lane) remains the publishing path; this module is the training
boundary.

Fidelity limits that are *recorded*, never silently repaired:

* Committed evidence bundles redact system/user prompts (``promote_codex_bundle``
  rule R1). A record whose model-visible context carries a redaction marker is
  quarantined, because the instruction *is* the training input.
* ATIF cannot say whether a harness presented a shell result as a ``user`` or a
  ``tool`` message (mini-swe-agent's converter folds both into
  ``observation.results``; only Responses-API results keep ``source_call_id``).
  Observations are exported under a neutral ``observation`` role with
  ``presented_as`` = ``user`` | ``tool`` | ``unknown``; raw mini messages
  (``agent/mini-swe-agent.trajectory.json``) settle it when present.
* Some ATIF writers store ``tool_calls[].arguments`` as a Python literal rather
  than JSON. The record keeps the original string and the re-parsed object with
  ``arguments_encoding`` naming the source encoding.
* No system prompt step means the harness did not capture one; the record says
  so (``fidelity.system_prompt_captured``) instead of inventing one.

Run with ``python -m evallab.sft_records export --root PATH [--root PATH ...]
--out DIR``. Output is deterministic for unchanged inputs and contains no
wall-clock values or absolute machine paths.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from evallab.interpretation.trajectory_hydration import _DEFAULT_SECRET_PATTERNS
from evallab.tracing import (
    REDACTION_MARKER_RE,
    TraceError,
    is_job_dir,
    is_trial_dir,
    load_trajectory,
    trajectory_path_for,
)
from evallab.training_eligibility import evaluate_trial

CONTRACT_VERSION = "evallab.sft_records/1"
RAW_MINI_RELATIVE = Path("agent") / "mini-swe-agent.trajectory.json"
FULL_RECORDS_FILE = "records.full.jsonl"
DECISION_RECORDS_FILE = "records.decisions.jsonl"
MANIFEST_FILE = "manifest.json"
CONTRACT_FILE = "contract.json"

Role = Literal["system", "user", "assistant", "observation"]
Disposition = Literal["accepted", "rejected", "quarantined", "duplicate"]

# Task lineage that must never reach a training set. Matched against the task
# name, the registry ``source_uri`` and the task package path.
_TB_LEAKAGE_RE = re.compile(
    r"(?<![a-z0-9])(?:tb|terminal[-_]bench)[-_]?[34](?![0-9])", re.IGNORECASE
)
# Seed/variant suffixes collapse into one conservative split group.
_SEED_SUFFIX_RE = re.compile(r"(?:-s\d+)?(?:-seed\d+)?$")

CONTRACT: dict[str, Any] = {
    "version": CONTRACT_VERSION,
    "files": {
        FULL_RECORDS_FILE: "one full-trajectory record per accepted trial (JSON per line)",
        DECISION_RECORDS_FILE: "one decision example per supervised assistant turn (JSON per line)",
        MANIFEST_FILE: "every discovered trial with its disposition and reasons; dedupe groups",
    },
    "roles": ["system", "user", "assistant", "observation"],
    "observation.presented_as": ["user", "tool", "unknown"],
    "supervision": {
        "targets": "indices into `messages` of assistant turns produced by `identity.actor`",
        "reasoning_content": "carried on the assistant message, excluded from supervision by default",
        "tool_calls[].arguments": "object when parseable; `arguments_raw` is the stored string",
    },
    "decision_example": {
        "context": "messages strictly before the target; observations of the target are absent",
        "target": "the supervised assistant message",
    },
    "lineage.training_allowed": "true | false | null (unknown; not in the registry)",
    "split.assignment": "train | selection | final | unassigned",
}


@dataclass(frozen=True)
class SourceRoot:
    label: str
    path: Path


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Any
    arguments_raw: str | None
    arguments_encoding: Literal["json", "python_literal", "object", "unparsed", "absent"]

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "arguments": self.arguments,
            "arguments_raw": self.arguments_raw,
            "arguments_encoding": self.arguments_encoding,
        }


@dataclass
class Message:
    role: Role
    content: str | None
    step_id: int | None
    reasoning_content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    presented_as: Literal["user", "tool", "unknown"] | None = None
    tool_call_id: str | None = None
    redacted: bool = False

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role, "content": self.content, "step_id": self.step_id}
        if self.role == "assistant":
            out["reasoning_content"] = self.reasoning_content
            out["tool_calls"] = [call.to_json() for call in self.tool_calls]
        if self.role == "observation":
            out["presented_as"] = self.presented_as
            out["tool_call_id"] = self.tool_call_id
        if self.redacted:
            out["redacted"] = True
        return out


@dataclass
class Fidelity:
    origin: Literal["atif", "raw_mini_messages"]
    system_prompt_captured: bool
    observation_role_known: bool
    tool_linkage: Literal["by_call_id", "positional", "none", "unmatched"]
    redacted_messages: list[int]
    reparsed_arguments: int
    secret_scan_hits: int
    limits: list[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "system_prompt_captured": self.system_prompt_captured,
            "observation_role_known": self.observation_role_known,
            "tool_linkage": self.tool_linkage,
            "redacted_messages": self.redacted_messages,
            "reparsed_arguments": self.reparsed_arguments,
            "secret_scan_hits": self.secret_scan_hits,
            "limits": self.limits,
        }


@dataclass
class TrialSource:
    root: SourceRoot
    trial_dir: Path
    result: dict[str, Any]
    trajectory: dict[str, Any] | None
    trajectory_sha256: str | None
    raw_mini: list[dict[str, Any]] | None
    raw_mini_sha256: str | None

    @property
    def relative_path(self) -> str:
        return self.trial_dir.relative_to(self.root.path).as_posix()

    @property
    def session_id(self) -> str | None:
        value = (self.trajectory or {}).get("session_id")
        return value if isinstance(value, str) and value else None

    @property
    def identity_key(self) -> str:
        """Source identity for dedupe: ATIF session, else trajectory digest.

        A trial with no trajectory has no shareable identity; it keeps a
        path-bound key so control trials never collapse into one group.
        """
        if self.session_id:
            return f"session:{self.session_id}"
        if self.trajectory_sha256:
            return f"sha256:{self.trajectory_sha256}"
        return f"path:{self.root.label}:{self.relative_path}"

    @property
    def is_redacted(self) -> bool:
        return isinstance((self.trajectory or {}).get("evallab_redaction"), dict)


@dataclass
class Outcome:
    reward: float | None
    verifier_present: bool
    exception_type: str | None
    failure_class: Literal["passed", "verifier_failed", "exception", "unverified"]

    def to_json(self) -> dict[str, Any]:
        return {
            "reward": self.reward,
            "verifier_present": self.verifier_present,
            "exception_type": self.exception_type,
            "failure_class": self.failure_class,
        }


@dataclass
class Lineage:
    task_name: str | None
    task_checksum: str | None
    task_family: str | None
    registry_task_id: str | None
    source_uri: str | None
    training_allowed: bool | None
    reasons: list[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "task_name": self.task_name,
            "task_checksum": self.task_checksum,
            "task_family": self.task_family,
            "registry_task_id": self.registry_task_id,
            "source_uri": self.source_uri,
            "training_allowed": self.training_allowed,
            "reasons": self.reasons,
        }


@dataclass
class Record:
    record_id: str
    source: dict[str, Any]
    identity: dict[str, Any]
    outcome: Outcome
    lineage: Lineage
    split: dict[str, Any]
    messages: list[Message]
    targets: list[int]
    fidelity: Fidelity
    usage: dict[str, int | None]

    def to_json(self) -> dict[str, Any]:
        return {
            "contract": CONTRACT_VERSION,
            "kind": "full_trajectory",
            "record_id": self.record_id,
            "source": self.source,
            "identity": self.identity,
            "outcome": self.outcome.to_json(),
            "lineage": self.lineage.to_json(),
            "split": self.split,
            "supervision_policy": {
                "assistant_content": "included",
                "tool_calls": "included",
                "reasoning_content": "excluded",
            },
            "messages": [message.to_json() for message in self.messages],
            "targets": self.targets,
            "fidelity": self.fidelity.to_json(),
            "usage": self.usage,
        }

    def decision_examples(self) -> Iterator[dict[str, Any]]:
        """One example per target: the exact preceding context and that turn.

        Observations belonging to the target come after it in ``messages``, so
        ``messages[:index]`` cannot contain them; earlier failed actions and their
        observations stay as unsupervised context.
        """
        base = self.to_json()
        for ordinal, index in enumerate(self.targets):
            yield {
                "contract": CONTRACT_VERSION,
                "kind": "decision_example",
                "record_id": f"{self.record_id}:d{ordinal}",
                "full_record_id": self.record_id,
                "source": base["source"],
                "identity": base["identity"],
                "outcome": base["outcome"],
                "lineage": base["lineage"],
                "split": base["split"],
                "supervision_policy": base["supervision_policy"],
                "context": base["messages"][:index],
                "target": base["messages"][index],
                "target_step_id": self.messages[index].step_id,
                "fidelity": base["fidelity"],
            }


@dataclass
class TrialDisposition:
    root: str
    trial: str
    identity_key: str
    disposition: Disposition
    reasons: list[str]
    record_id: str | None = None
    duplicate_of: str | None = None

    def to_json(self) -> dict[str, Any]:
        out = {
            "root": self.root,
            "trial": self.trial,
            "identity_key": self.identity_key,
            "disposition": self.disposition,
            "reasons": self.reasons,
            "record_id": self.record_id,
        }
        if self.duplicate_of is not None:
            out["duplicate_of"] = self.duplicate_of
        return out


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def discover_trial_dirs(root: Path) -> list[Path]:
    """Every trial directory under ``root`` at any depth, sorted, hidden dirs skipped.

    A root may itself be a trial, a job, or a dated tree of jobs. Job-level
    artifacts (``config.json``, ``job.log``) are never mistaken for trials.
    """
    if not root.is_dir():
        raise TraceError(f"root is not a directory: {root.resolve()}")
    found: list[Path] = []
    if is_trial_dir(root) and not is_job_dir(root):
        return [root]
    for current, dirnames, _ in os.walk(root):
        here = Path(current)
        dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
        if here != root and is_trial_dir(here) and not is_job_dir(here):
            found.append(here)
            dirnames[:] = []  # trials do not nest
    return sorted(found)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def load_trial_source(root: SourceRoot, trial_dir: Path) -> TrialSource:
    result = _read_json(trial_dir / "result.json")
    result = result if isinstance(result, dict) else {}
    trajectory_path = trajectory_path_for(trial_dir)
    trajectory: dict[str, Any] | None = None
    trajectory_sha256: str | None = None
    if trajectory_path.is_file():
        trajectory = load_trajectory(trajectory_path)
        trajectory_sha256 = _sha256_file(trajectory_path)
    raw_path = trial_dir / RAW_MINI_RELATIVE
    raw_mini: list[dict[str, Any]] | None = None
    raw_mini_sha256: str | None = None
    if raw_path.is_file():
        loaded = _read_json(raw_path)
        messages = loaded.get("messages") if isinstance(loaded, dict) else None
        if isinstance(messages, list):
            raw_mini = messages
            raw_mini_sha256 = _sha256_file(raw_path)
    return TrialSource(
        root=root,
        trial_dir=trial_dir,
        result=result,
        trajectory=trajectory,
        trajectory_sha256=trajectory_sha256,
        raw_mini=raw_mini,
        raw_mini_sha256=raw_mini_sha256,
    )


# ---------------------------------------------------------------------------
# Lineage and splits
# ---------------------------------------------------------------------------


def load_registry(registry_dir: Path | None) -> dict[str, dict[str, Any]]:
    """Registry cards keyed by ``task_id``; an absent directory means no lineage."""
    cards: dict[str, dict[str, Any]] = {}
    if registry_dir is None or not registry_dir.is_dir():
        return cards
    for path in sorted(registry_dir.glob("*.json")):
        card = _read_json(path)
        if isinstance(card, dict) and isinstance(card.get("task_id"), str):
            cards[card["task_id"]] = card
    return cards


def _task_short_name(task_name: str | None) -> str | None:
    if not task_name:
        return None
    return task_name.rsplit("/", 1)[-1]


def resolve_lineage(source: TrialSource, registry: dict[str, dict[str, Any]]) -> Lineage:
    task_name = source.result.get("task_name")
    task_name = task_name if isinstance(task_name, str) else None
    checksum = source.result.get("task_checksum")
    checksum = checksum if isinstance(checksum, str) else None
    task_id = source.result.get("task_id")
    task_path = task_id.get("path") if isinstance(task_id, dict) else None
    short = _task_short_name(task_name)
    card = registry.get(short) if short else None
    if card is None and short:
        # Registry ids are unseeded family names; a seeded task name still belongs.
        card = registry.get(_SEED_SUFFIX_RE.sub("", short))
    reasons: list[str] = []
    training_allowed: bool | None = None
    family: str | None = None
    source_uri: str | None = None
    if card is not None:
        family = card.get("task_family") if isinstance(card.get("task_family"), str) else None
        source_uri = card.get("source_uri") if isinstance(card.get("source_uri"), str) else None
        uses = card.get("allowed_uses")
        training_allowed = isinstance(uses, list) and "training" in uses
        if not training_allowed:
            reasons.append("registry_disallows_training")
    else:
        reasons.append("task_not_in_registry")
    for candidate in (task_name, source_uri, task_path if isinstance(task_path, str) else None):
        if candidate and _TB_LEAKAGE_RE.search(candidate):
            training_allowed = False
            reasons.append("terminal_bench_3_4_lineage")
            break
    if family is None and short:
        family = _SEED_SUFFIX_RE.sub("", short)
    return Lineage(
        task_name=task_name,
        task_checksum=checksum,
        task_family=family,
        registry_task_id=card.get("task_id") if card else None,
        source_uri=source_uri,
        training_allowed=training_allowed,
        reasons=reasons,
    )


def load_split_manifest(path: Path | None) -> dict[str, str]:
    """``{group_or_task_name: assignment}``; whole families move together."""
    if path is None:
        return {}
    loaded = _read_json(path)
    if not isinstance(loaded, dict):
        raise TraceError(f"split manifest must be a JSON object: {path}")
    allowed = {"train", "selection", "final"}
    out: dict[str, str] = {}
    for key, value in loaded.items():
        if not isinstance(key, str) or value not in allowed:
            raise TraceError(
                f"split manifest entry {key!r} -> {value!r} is not train|selection|final"
            )
        out[key] = value
    return out


def resolve_split(
    lineage: Lineage, manifest: dict[str, str], manifest_label: str | None
) -> dict[str, Any]:
    group = lineage.task_family
    assignment = "unassigned"
    for key in (lineage.task_name, _task_short_name(lineage.task_name), group):
        if key and key in manifest:
            assignment = manifest[key]
            break
    return {
        "group": group,
        "assignment": assignment,
        "source": manifest_label if assignment != "unassigned" else None,
    }


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def _text_of(message: Any) -> tuple[str | None, bool]:
    """(text, multimodal) for an ATIF message which may be a string or text parts."""
    if message is None:
        return None, False
    if isinstance(message, str):
        return message, False
    if isinstance(message, list):
        parts: list[str] = []
        for part in message:
            if (
                isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            ):
                parts.append(part["text"])
            else:
                return None, True
        return "\n".join(parts), False
    return None, True


def _has_redaction(text: str | None) -> bool:
    return bool(text) and REDACTION_MARKER_RE.search(text) is not None


def parse_tool_call(raw: dict[str, Any]) -> ToolCall:
    args = raw.get("arguments")
    if args is None:
        parsed, raw_text, encoding = None, None, "absent"
    elif isinstance(args, dict | list):
        parsed, raw_text, encoding = args, None, "object"
    elif isinstance(args, str):
        raw_text = args
        try:
            parsed, encoding = json.loads(args), "json"
        except ValueError:
            try:
                literal = ast.literal_eval(args)
            except (ValueError, SyntaxError):
                parsed, encoding = None, "unparsed"
            else:
                if isinstance(literal, dict | list):
                    parsed, encoding = literal, "python_literal"
                else:
                    parsed, encoding = None, "unparsed"
    else:
        # Non-string, non-container arguments: keep no text, mark unsupported.
        parsed, raw_text, encoding = None, None, "unparsed"
    return ToolCall(
        id=str(raw.get("tool_call_id") or ""),
        name=str(raw.get("function_name") or ""),
        arguments=parsed,
        arguments_raw=raw_text,
        arguments_encoding=encoding,
    )


def _raw_mini_observation_roles(raw_mini: list[dict[str, Any]] | None) -> list[str] | None:
    """Presented roles of every shell result, in order, from raw mini messages.

    mini-swe-agent puts the task in message index 1; every later ``user`` or
    ``tool`` message is a shell result the model saw under that role.
    """
    if raw_mini is None:
        return None
    roles: list[str] = []
    for index, message in enumerate(raw_mini):
        if not isinstance(message, dict):
            return None
        role = message.get("role")
        if role == "user" and index > 1:
            roles.append("user")
        elif role == "tool" or message.get("type") == "function_call_output":
            roles.append("tool")
    return roles


def convert_steps(
    steps: list[dict[str, Any]],
    *,
    raw_mini: list[dict[str, Any]] | None,
) -> tuple[list[Message], Fidelity, list[str]]:
    """ATIF steps -> neutral messages. Returns (messages, fidelity, reject_reasons)."""
    messages: list[Message] = []
    rejects: list[str] = []
    limits: list[str] = []
    redacted: list[int] = []
    reparsed = 0
    linkage_kinds: set[str] = set()
    presented_roles = _raw_mini_observation_roles(raw_mini)
    observation_cursor = 0
    role_known = presented_roles is not None
    system_seen = False

    for step in steps:
        source = step.get("source")
        step_id = step.get("step_id") if isinstance(step.get("step_id"), int) else None
        if source in ("system", "user"):
            text, multimodal = _text_of(step.get("message"))
            if multimodal:
                rejects.append("unsupported_multimodal_content")
                continue
            role: Role = "system" if source == "system" else "user"
            system_seen |= role == "system"
            message = Message(
                role=role, content=text, step_id=step_id, redacted=_has_redaction(text)
            )
            if message.redacted:
                redacted.append(len(messages))
            messages.append(message)
            continue
        if source != "agent":
            limits.append(f"ignored_step_source:{source}")
            continue
        text, multimodal = _text_of(step.get("message"))
        if multimodal:
            rejects.append("unsupported_multimodal_content")
            continue
        reasoning = step.get("reasoning_content")
        calls = [
            parse_tool_call(raw) for raw in step.get("tool_calls") or [] if isinstance(raw, dict)
        ]
        for call in calls:
            if call.arguments_encoding == "python_literal":
                reparsed += 1
            elif call.arguments_encoding == "unparsed":
                rejects.append("malformed_tool_arguments")
        messages.append(
            Message(
                role="assistant",
                content=text,
                step_id=step_id,
                reasoning_content=reasoning if isinstance(reasoning, str) else None,
                tool_calls=calls,
            )
        )
        observation = step.get("observation")
        results = observation.get("results") if isinstance(observation, dict) else None
        if not isinstance(results, list) or not results:
            continue
        call_ids = [call.id for call in calls if call.id]
        for position, result in enumerate(results):
            if not isinstance(result, dict):
                rejects.append("malformed_observation_result")
                continue
            obs_text, multimodal = _text_of(result.get("content"))
            if multimodal:
                rejects.append("unsupported_multimodal_content")
                continue
            source_call_id = result.get("source_call_id")
            if isinstance(source_call_id, str) and source_call_id:
                linkage_kinds.add("by_call_id" if source_call_id in call_ids else "unmatched")
                tool_call_id: str | None = source_call_id
            elif calls and len(results) == len(calls):
                linkage_kinds.add("positional")
                tool_call_id = call_ids[position] if position < len(call_ids) else None
            elif calls:
                linkage_kinds.add("unmatched")
                tool_call_id = None
            else:
                linkage_kinds.add("none")
                tool_call_id = None
            presented: Literal["user", "tool", "unknown"]
            if presented_roles is not None and observation_cursor < len(presented_roles):
                presented = "tool" if presented_roles[observation_cursor] == "tool" else "user"
            elif presented_roles is not None:
                presented, role_known = "unknown", False
            else:
                presented = "unknown"
            observation_cursor += 1
            message = Message(
                role="observation",
                content=obs_text,
                step_id=step_id,
                presented_as=presented,
                tool_call_id=tool_call_id,
                redacted=_has_redaction(obs_text),
            )
            if message.redacted:
                redacted.append(len(messages))
            messages.append(message)

    if presented_roles is not None and observation_cursor != len(presented_roles):
        role_known = False
        limits.append("raw_mini_observation_count_mismatch")
    if "unmatched" in linkage_kinds:
        rejects.append("malformed_tool_linkage")
        linkage: str = "unmatched"
    elif "by_call_id" in linkage_kinds:
        linkage = "by_call_id"
    elif "positional" in linkage_kinds:
        linkage = "positional"
    else:
        linkage = "none"
    if not system_seen:
        limits.append("system_prompt_not_captured")
    if not role_known:
        limits.append("observation_presented_role_unknown")
    fidelity = Fidelity(
        origin="raw_mini_messages" if raw_mini is not None else "atif",
        system_prompt_captured=system_seen,
        observation_role_known=role_known,
        tool_linkage=linkage,  # type: ignore[arg-type]
        redacted_messages=redacted,
        reparsed_arguments=reparsed,
        secret_scan_hits=0,
        limits=limits,
    )
    return messages, fidelity, sorted(set(rejects))


def _scan_secrets(messages: Iterable[Message]) -> int:
    hits = 0
    for message in messages:
        for text in (
            message.content,
            message.reasoning_content,
            *(c.arguments_raw for c in message.tool_calls),
        ):
            if not text:
                continue
            hits += sum(1 for pattern in _DEFAULT_SECRET_PATTERNS if pattern.search(text))
    return hits


def resolve_outcome(source: TrialSource) -> Outcome:
    verifier = source.result.get("verifier_result")
    rewards = verifier.get("rewards") if isinstance(verifier, dict) else None
    reward: float | None = None
    if isinstance(rewards, dict) and isinstance(rewards.get("reward"), int | float):
        reward = float(rewards["reward"])
    exception = source.result.get("exception_info")
    exception_type = None
    if isinstance(exception, dict):
        exception_type = str(
            exception.get("exception_type") or exception.get("type") or "exception"
        )
    if exception_type:
        failure = "exception"
    elif reward is None:
        failure = "unverified"
    elif reward >= 1.0:
        failure = "passed"
    else:
        failure = "verifier_failed"
    return Outcome(
        reward=reward,
        verifier_present=isinstance(verifier, dict),
        exception_type=exception_type,
        failure_class=failure,  # type: ignore[arg-type]
    )


def build_record(
    source: TrialSource,
    *,
    registry: dict[str, dict[str, Any]],
    split_manifest: dict[str, str],
    split_label: str | None,
) -> tuple[Record | None, Disposition, list[str]]:
    """Convert one trial. Rejections are structural; quarantines are fidelity."""
    eligibility = evaluate_trial(source.trial_dir)
    early = [r for r in eligibility["sft"]["reasons"] if r != "unsupported_agent_output"]
    if early:
        return None, "rejected", early
    assert source.trajectory is not None
    steps = source.trajectory.get("steps") or []
    messages, fidelity, rejects = convert_steps(steps, raw_mini=source.raw_mini)
    # ``unsupported_agent_output`` covers formats the bridge itself can
    # adjudicate: string tool arguments it reparses (kept, flagged) or
    # multimodal parts it rejects below. Only formats the bridge cannot
    # faithfully represent remain rejects.
    late = [
        reason
        for reason in eligibility["sft"]["reasons"]
        if reason == "unsupported_agent_output" and rejects
    ]
    if late or rejects:
        return None, "rejected", sorted(set(late + rejects))
    targets = [index for index, message in enumerate(messages) if message.role == "assistant"]
    if not targets:
        return None, "rejected", ["no_agent_steps"]
    lineage = resolve_lineage(source, registry)
    if lineage.training_allowed is False:
        return None, "rejected", list(lineage.reasons)
    fidelity.secret_scan_hits = _scan_secrets(messages)
    quarantine: list[str] = []
    if fidelity.redacted_messages:
        quarantine.append("redacted_model_visible_context")
    if fidelity.secret_scan_hits:
        quarantine.append("secret_pattern_in_context")
    agent_raw = source.trajectory.get("agent")
    agent: dict[str, Any] = agent_raw if isinstance(agent_raw, dict) else {}
    config_agent = (source.result.get("config") or {}).get("agent")
    config_agent = config_agent if isinstance(config_agent, dict) else {}
    identity = {
        "actor": eligibility["identity"]["model_name"],
        "model_name": eligibility["identity"]["model_name"],
        "reasoning_effort": eligibility["identity"]["reasoning_effort"],
        "tool_protocol": eligibility["identity"]["tool_protocol"],
        "harness": {"name": agent.get("name"), "version": agent.get("version")},
        "harness_config_name": config_agent.get("name"),
    }
    digest_basis = source.trajectory_sha256 or ""
    record_id = hashlib.sha256(f"{source.identity_key}|{digest_basis}".encode()).hexdigest()[:24]
    record = Record(
        record_id=record_id,
        source={
            "root": source.root.label,
            "trial": source.relative_path,
            "job": source.trial_dir.parent.name,
            "trial_name": source.trial_dir.name,
            "session_id": source.session_id,
            "trajectory_sha256": source.trajectory_sha256,
            "raw_mini_sha256": source.raw_mini_sha256,
            "redacted_bundle": source.is_redacted,
        },
        identity=identity,
        outcome=resolve_outcome(source),
        lineage=lineage,
        split=resolve_split(lineage, split_manifest, split_label),
        messages=messages,
        targets=targets,
        fidelity=fidelity,
        usage=eligibility["usage"],
    )
    return record, ("quarantined" if quarantine else "accepted"), quarantine


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


@dataclass
class ExportResult:
    dispositions: list[TrialDisposition]
    accepted: list[Record]
    quarantined: list[Record]
    duplicates: dict[str, list[str]]

    def counts(self) -> dict[str, int]:
        counter: dict[str, int] = {}
        for item in self.dispositions:
            counter[item.disposition] = counter.get(item.disposition, 0) + 1
        return counter


def export_records(
    roots: list[SourceRoot],
    *,
    registry_dir: Path | None,
    split_manifest_path: Path | None,
) -> ExportResult:
    registry = load_registry(registry_dir)
    split_manifest = load_split_manifest(split_manifest_path)
    split_label = split_manifest_path.name if split_manifest_path else None

    sources: list[TrialSource] = []
    for root in roots:
        for trial_dir in discover_trial_dirs(root.path):
            sources.append(load_trial_source(root, trial_dir))

    # Dedupe by source identity. Prefer an unredacted copy; otherwise root order.
    by_identity: dict[str, list[TrialSource]] = {}
    for source in sources:
        by_identity.setdefault(source.identity_key, []).append(source)
    chosen: dict[str, TrialSource] = {}
    duplicates: dict[str, list[str]] = {}
    for key, group in by_identity.items():
        ordered = sorted(group, key=lambda s: (s.is_redacted, roots.index(s.root), s.relative_path))
        chosen[key] = ordered[0]
        if len(ordered) > 1:
            duplicates[key] = [f"{s.root.label}:{s.relative_path}" for s in ordered[1:]]

    dispositions: list[TrialDisposition] = []
    accepted: list[Record] = []
    quarantined: list[Record] = []
    for source in sources:
        winner = chosen[source.identity_key]
        if winner is not source:
            dispositions.append(
                TrialDisposition(
                    root=source.root.label,
                    trial=source.relative_path,
                    identity_key=source.identity_key,
                    disposition="duplicate",
                    reasons=["same_source_identity"],
                    duplicate_of=f"{winner.root.label}:{winner.relative_path}",
                )
            )
            continue
        try:
            record, disposition, reasons = build_record(
                source, registry=registry, split_manifest=split_manifest, split_label=split_label
            )
        except TraceError as exc:
            record, disposition, reasons = None, "rejected", [f"trace_error:{exc}"]
        dispositions.append(
            TrialDisposition(
                root=source.root.label,
                trial=source.relative_path,
                identity_key=source.identity_key,
                disposition=disposition,
                reasons=reasons,
                record_id=record.record_id if record else None,
            )
        )
        if record is None:
            continue
        if disposition == "accepted":
            accepted.append(record)
        else:
            quarantined.append(record)
    accepted.sort(
        key=lambda r: (r.lineage.task_name or "", r.source["session_id"] or "", r.record_id)
    )
    quarantined.sort(
        key=lambda r: (r.lineage.task_name or "", r.source["session_id"] or "", r.record_id)
    )
    dispositions.sort(key=lambda d: (d.root, d.trial))
    return ExportResult(
        dispositions=dispositions, accepted=accepted, quarantined=quarantined, duplicates=duplicates
    )


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            count += 1
    return count


def write_export(result: ExportResult, out_dir: Path, *, roots: list[SourceRoot]) -> dict[str, Any]:
    """Write records, quarantine, manifest and contract. Refuses a non-empty output dir."""
    if out_dir.exists() and any(out_dir.iterdir()):
        raise TraceError(f"output directory is not empty: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    full_count = _write_jsonl(out_dir / FULL_RECORDS_FILE, (r.to_json() for r in result.accepted))
    decision_count = _write_jsonl(
        out_dir / DECISION_RECORDS_FILE,
        (ex for r in result.accepted for ex in r.decision_examples()),
    )
    quarantine_dir = out_dir / "quarantine"
    quarantine_dir.mkdir()
    quarantine_count = _write_jsonl(
        quarantine_dir / FULL_RECORDS_FILE, (r.to_json() for r in result.quarantined)
    )
    reason_counts: dict[str, int] = {}
    for item in result.dispositions:
        for reason in item.reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    manifest = {
        "contract": CONTRACT_VERSION,
        "roots": [{"label": root.label, "path": root.path.as_posix()} for root in roots],
        "counts": {
            **result.counts(),
            "full_records": full_count,
            "decision_examples": decision_count,
            "quarantined_records": quarantine_count,
        },
        "reason_counts": dict(sorted(reason_counts.items())),
        "duplicates": dict(sorted(result.duplicates.items())),
        "trials": [item.to_json() for item in result.dispositions],
    }
    (out_dir / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (out_dir / CONTRACT_FILE).write_text(json.dumps(CONTRACT, indent=2, sort_keys=True) + "\n")
    return manifest


def _parse_root(value: str) -> SourceRoot:
    label, sep, path = value.partition("=")
    if not sep:
        path, label = value, Path(value).name
    return SourceRoot(label=label, path=Path(path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="export records from one or more roots")
    export.add_argument(
        "--root",
        action="append",
        required=True,
        type=_parse_root,
        help="LABEL=PATH (or PATH; label defaults to the directory name); repeatable",
    )
    export.add_argument("--out", type=Path, required=True, help="new, empty output directory")
    export.add_argument(
        "--registry",
        type=Path,
        default=Path("library/registry"),
        help="task registry directory for lineage",
    )
    export.add_argument(
        "--split-manifest",
        type=Path,
        default=None,
        help="JSON {task_or_family: train|selection|final}",
    )
    args = parser.parse_args(argv)
    try:
        result = export_records(
            args.root, registry_dir=args.registry, split_manifest_path=args.split_manifest
        )
        manifest = write_export(result, args.out, roots=args.root)
    except TraceError as exc:
        print(f"error: {exc}")
        return 2
    print(
        json.dumps(
            {
                "out": args.out.as_posix(),
                "counts": manifest["counts"],
                "reasons": manifest["reason_counts"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
