"""Bounded trace projections and root-confined DEV-only grader comparisons.

The journal supplies evidence, not filesystem authority. Grader inspection resolves
all recorded corpus identities before opening task files, then confines every path
(including symlinks) to operator-configured roots. No verifier executes here.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import math
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from evallab.production.corpus_queue import CorpusItem, CorpusRegistry
from evallab.production.safe_paths import read_bounded_text, resolve_within

if TYPE_CHECKING:
    from evallab.production.control_plane import EventJournal

_TEXT_LIMIT = 16384
_FILE_LIMIT = 65536
_SKIP_DIRECTORIES = frozenset({".git", "__pycache__", "node_modules"})
_CONTROL_STATES = frozenset({"requested", "acknowledged", "applied", "expired"})
_ACTION_STATES = {
    "pause": "paused",
    "cancel": "stopped",
    "stop": "stopped",
    "quarantine": "quarantined",
}
_TERMINAL = (".completed", ".finished", ".failed", ".stopped", ".cancelled", ".canceled")
_TOKEN = re.compile(r"[\w-]+")
_SEALED_MESSAGE = "split_sealed: inspection contains sealed corpus material"


class AmbiguousRunError(ValueError):
    """A campaign needs a task selector before its evidence can be inspected."""


@runtime_checkable
class _RunEventJournal(Protocol):
    def read_run_events(
        self, run_id: str, *, after_seq: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]: ...


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _payload(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = _mapping(event.get("payload"))
    return {**payload, **_mapping(payload.get("asset"))}


def _text(value: Any, limit: int = _TEXT_LIMIT) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text.encode("utf-8", errors="replace")[:limit].decode("utf-8", errors="ignore")


def _number(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _step_id(value: Any) -> str | int | None:
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    if isinstance(value, str) and value.startswith("head#") and value[5:].isdecimal():
        return int(value[5:])
    return value if isinstance(value, (str, int)) and not isinstance(value, bool) else None


def _raw_steps(payload: Mapping[str, Any], *, kind: str = "") -> list[Any]:
    if kind in {"seeker.attempt_started", "seeker.grader_feedback"}:
        return []
    steps = payload.get("steps")
    if not isinstance(steps, list):
        partial = payload.get("partial_trace")
        steps = partial.get("steps") if isinstance(partial, Mapping) else partial
    if not isinstance(steps, list):
        steps = [payload["step"]] if isinstance(payload.get("step"), Mapping) else []
    if not steps and kind == "trace.step" and "step_id" in payload:
        steps = [payload]
    return steps


def _citations(payload: Mapping[str, Any]) -> list[str | int]:
    finding = _mapping(payload.get("finding"))
    citations: list[str | int] = []
    for source in (payload, finding):
        for key in ("step_citations", "evidence"):
            evidence_items = source.get(key)
            for evidence in evidence_items if isinstance(evidence_items, list) else []:
                if isinstance(evidence, Mapping):
                    evidence = evidence.get(
                        "step", evidence.get("step_id", evidence.get("step_ref"))
                    )
                step = _step_id(evidence)
                if step is not None and step not in citations:
                    citations.append(step)
    return citations


@dataclass(frozen=True)
class _File:
    path: Path
    digest: str
    binary: bool


def _objects(value: Any) -> Iterator[Mapping[str, Any]]:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            yield current
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)


class _SealedContentGuard:
    """Match sealed identifiers at token boundaries without echoing their values."""

    def __init__(self, registry: CorpusRegistry) -> None:
        tokens: set[tuple[str, bool]] = set()
        for item in registry.items(split="test"):
            tokens.add((item.item_id, False))
            if item.task_id:
                tokens.add((item.task_id, False))
                tokens.add((item.task_id.split("@", 1)[0].rsplit("/", 1)[-1], False))
            if item.task_digest:
                tokens.add((item.task_digest, True))
                tokens.add((item.task_digest.removeprefix("sha256:"), True))
        self._indices: list[dict[str, list[tuple[str, int]]]] = [{}, {}]
        self._punctuation: list[re.Pattern[str]] = []
        for token, insensitive in tokens:
            if not token:
                continue
            pieces = list(_TOKEN.finditer(token))
            if pieces:
                anchor = max(pieces, key=lambda piece: len(piece[0]))
                key = anchor[0].casefold() if insensitive else anchor[0]
                self._indices[int(insensitive)].setdefault(key, []).append((token, anchor.start()))
            else:
                self._punctuation.append(
                    re.compile(
                        r"(?<![\w-])" + re.escape(token) + r"(?![\w-])",
                        re.IGNORECASE if insensitive else 0,
                    )
                )

    def _matches(self, text: str) -> bool:
        for word in _TOKEN.finditer(text):
            for insensitive, index in enumerate(self._indices):
                key = word[0].casefold() if insensitive else word[0]
                for token, offset in index.get(key, ()):
                    start, end = word.start() - offset, word.start() - offset + len(token)
                    if start < 0 or end > len(text):
                        continue
                    if start and (text[start - 1].isalnum() or text[start - 1] in "_-"):
                        continue
                    if end < len(text) and (text[end].isalnum() or text[end] in "_-"):
                        continue
                    candidate = text[start:end]
                    if candidate == token or (
                        insensitive and candidate.casefold() == token.casefold()
                    ):
                        return True
        return any(pattern.search(text) for pattern in self._punctuation)

    def check(self, value: Any) -> None:
        pending = [value]
        while pending:
            current = pending.pop()
            if isinstance(current, Mapping):
                pending.extend(current.keys())
                pending.extend(current.values())
            elif isinstance(current, (list, tuple)):
                pending.extend(current)
            elif isinstance(current, str) and self._matches(current):
                raise PermissionError(_SEALED_MESSAGE)


class RunInspector:
    """Read-only inspection over the durable queue, never its bounded UI cache."""

    def __init__(
        self,
        journal: EventJournal,
        registry: CorpusRegistry,
        *,
        allowed_roots: Sequence[Path],
    ) -> None:
        self.journal = journal
        self.registry = registry
        self.allowed_roots = tuple(allowed_roots)
        self._seal = _SealedContentGuard(registry)

    def _events(self, run_id: str | None = None) -> Iterator[dict[str, Any]]:
        after_seq = 0
        high_water = int(self.journal.queue_snapshot()["last_seq"])
        while after_seq < high_water:
            if run_id is not None and isinstance(self.journal, _RunEventJournal):
                page = self.journal.read_run_events(run_id, after_seq=after_seq, limit=500)
            else:
                page = self.journal.read_events(after_seq=after_seq, limit=500)
            if not page:
                break
            for event in page:
                seq = int(event["seq"])
                if seq > high_water:
                    return
                if seq <= after_seq:
                    raise RuntimeError("journal did not advance its sequence cursor")
                after_seq = seq
                if run_id is None or event["run_id"] == run_id:
                    yield event

    def _lineage(
        self, events: Sequence[Mapping[str, Any]]
    ) -> tuple[dict[tuple[str, str], CorpusItem], dict[str, frozenset[tuple[str, str]]]]:
        """Validate the whole source graph before resolving each event's task."""
        pending = list(reversed(events))
        sources: dict[str, Mapping[str, Any]] = {}
        references: dict[str, list[str]] = {}
        identities: dict[str, frozenset[tuple[str, str]]] = {}
        resolved_items: dict[tuple[str, str], CorpusItem] = {}
        while pending:
            event = pending.pop()
            event_id = str(event["event_id"])
            if event_id in sources:
                continue
            sources[event_id] = event
            self._seal.check(event.get("payload"))
            event_items: set[tuple[str, str]] = set()
            event_references: list[str] = []
            for payload in _objects(event.get("payload")):
                if "dataset_id" in payload or "item_id" in payload:
                    dataset_id, item_id = payload.get("dataset_id"), payload.get("item_id")
                    if not isinstance(dataset_id, str) or not isinstance(item_id, str):
                        raise PermissionError("split_sealed: corpus lineage is unresolved")
                    key = dataset_id, item_id
                    if key not in resolved_items:
                        try:
                            resolved = self.registry.get(*key)
                        except KeyError:
                            raise PermissionError(
                                "split_sealed: corpus lineage is unresolved"
                            ) from None
                        if resolved.split != "dev":
                            raise PermissionError("split_sealed: effective corpus split is not DEV")
                        resolved_items[key] = resolved
                    event_items.add(key)
                event_references.extend(
                    payload[key]
                    for key in ("source_event_id", "qualification_event_id", "job_id")
                    if isinstance(payload.get(key), str) and payload[key]
                )
                for key in ("source_event_ids", "provenance_event_ids"):
                    values = payload.get(key)
                    if isinstance(values, list):
                        event_references.extend(
                            value for value in values if isinstance(value, str) and value
                        )
            references[event_id] = list(dict.fromkeys(event_references))
            for reference in references[event_id]:
                if reference in sources:
                    continue
                source = self.journal.get_event(reference)
                if source is None:
                    raise PermissionError("split_sealed: referenced lineage is unresolved")
                pending.append(source)
            # A direct task claim is authoritative for attribution, but never
            # exempts any other nested/source provenance from DEV validation.
            payload = _mapping(event.get("payload"))
            primary = {
                (value["dataset_id"], value["item_id"])
                for value in (payload, _mapping(payload.get("asset")))
                if "dataset_id" in value and "item_id" in value
            }
            # Without a direct claim, follow the source before treating nested
            # provenance as the task itself. Ancillary DEV references may differ.
            identities[event_id] = frozenset(
                primary or (event_items if not references[event_id] else ())
            )
        attributed = {event_id: pairs for event_id, pairs in identities.items() if pairs}
        for root in events:
            root_id = str(root["event_id"])
            if root_id in attributed:
                continue
            pending_ids = [root_id]
            seen: set[str] = set()
            pairs: set[tuple[str, str]] = set()
            while pending_ids:
                event_id = pending_ids.pop()
                if event_id in seen:
                    continue
                seen.add(event_id)
                if event_id in attributed:
                    pairs.update(attributed[event_id])
                else:
                    pending_ids.extend(references[event_id])
            attributed[root_id] = frozenset(pairs)
        return resolved_items, attributed

    def _selector(self, dataset_id: str | None, item_id: str | None) -> tuple[str, str] | None:
        if (dataset_id is None) != (item_id is None):
            raise ValueError("dataset_id and item_id must be supplied together")
        if dataset_id is None:
            return None
        if (
            not isinstance(dataset_id, str)
            or not dataset_id.strip()
            or not isinstance(item_id, str)
            or not item_id.strip()
        ):
            raise ValueError("task selectors must be nonblank strings")
        self._seal.check({"dataset_id": dataset_id, "item_id": item_id})
        return dataset_id, item_id

    def _select_events(
        self,
        events: Sequence[dict[str, Any]],
        selector: tuple[str, str] | None,
        *,
        asset: Mapping[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], CorpusItem]:
        items, attributed = self._lineage(events)
        run_items = {pair for event in events for pair in attributed[str(event["event_id"])]}
        if not run_items:
            raise PermissionError("split_sealed: registered DEV corpus lineage is required")
        if asset is not None:
            asset_items = attributed[str(asset["event_id"])]
            if len(asset_items) == 1:
                selector = next(iter(asset_items))
            elif asset_items or len(run_items) > 1:
                raise ValueError("asset task identity is unresolved or ambiguous")
        if selector is None:
            if len(run_items) > 1:
                raise AmbiguousRunError(
                    "Campaign contains multiple tasks; supply dataset_id and item_id together"
                )
            selector = next(iter(run_items))
        if selector not in run_items:
            raise LookupError("selected task item does not belong to this run")
        selected = []
        for event in events:
            kind = str(event["event_type"])
            if kind == "operator.control_requested" or kind.startswith("control."):
                selected.append(event)
                continue
            pairs = attributed[str(event["event_id"])]
            if len(pairs) > 1 and selector in pairs:
                raise ValueError("event task identity is ambiguous")
            if pairs == {selector} or (not pairs and len(run_items) == 1):
                selected.append(event)
        return selected, items[selector]

    def _response(self, value: dict[str, Any]) -> dict[str, Any]:
        self._seal.check(value)
        return value

    def timeline(
        self,
        run_id: str,
        *,
        max_steps: int = 500,
        dataset_id: str | None = None,
        item_id: str | None = None,
    ) -> dict[str, Any]:
        """Fold one DEV item's evidence and the campaign-wide control transitions."""
        if type(max_steps) is not int or max_steps < 1:
            raise ValueError("max_steps must be a positive integer")
        selector = self._selector(dataset_id, item_id)
        self._seal.check(run_id)
        events = list(self._events(run_id))
        if not events:
            raise LookupError(run_id)
        events, item = self._select_events(events, selector)
        steps: dict[str | int, dict[str, Any]] = {}
        prefixes: list[dict[str, Any]] = []
        cards: dict[str, dict[str, Any]] = {}
        gates: list[dict[str, Any]] = []
        controls: dict[str, dict[str, Any]] = {}
        tripwires: dict[str, list[str | int]] = {}
        run_state = "running"
        source_kind: str | None = None
        for event in events:
            payload, kind = _payload(event), str(event["event_type"])
            if kind == "operator.control_requested" or kind.startswith("control."):
                state = (
                    "requested"
                    if kind == "operator.control_requested"
                    else kind.removeprefix("control.")
                )
                if state not in _CONTROL_STATES:
                    continue
                request_id = str(payload.get("request_id") or event["event_id"])
                previous = controls.get(request_id, {})
                if previous.get("state") in {"applied", "expired"} and state != previous["state"]:
                    continue
                control = {**previous, **payload}
                controls[request_id] = {
                    "request_id": request_id,
                    "action": _text(control.get("action")) or "unknown",
                    "state": state,
                    "operator": _text(control.get("operator")),
                    "reason": _text(control.get("reason")),
                    "event_id": event["event_id"],
                    "seq": event["seq"],
                }
                if state == "applied":
                    run_state = str(
                        payload.get("run_state")
                        or _ACTION_STATES.get(control.get("action"), "running")
                    )
                continue
            if isinstance(payload.get("source_kind"), str):
                source_kind = _text(payload["source_kind"])
            meta = {key: event[key] for key in ("seq", "event_id", "ts")}
            for raw in _raw_steps(payload, kind=kind):
                if not isinstance(raw, Mapping):
                    continue
                step_id = _step_id(raw.get("step_id", raw.get("id", event.get("prefix_step"))))
                if step_id is None:
                    continue
                previous = steps.get(step_id, {})
                step = {**previous, **raw}
                recorded_ts = raw.get(
                    "recorded_ts", payload.get("recorded_ts", previous.get("recorded_ts"))
                )
                calls = []
                raw_calls = step.get("tool_calls")
                for call in raw_calls if isinstance(raw_calls, list) else []:
                    if isinstance(call, Mapping):
                        calls.append(
                            {
                                "tool_call_id": _text(call.get("tool_call_id")),
                                "function_name": _text(call.get("function_name")),
                                "arguments": _text(call.get("arguments")) or "",
                            }
                        )
                steps[step_id] = {
                    **meta,
                    "step_id": step_id,
                    "system": event["system"],
                    "source": _text(step.get("source")) or "unknown",
                    "recorded_ts": _text(recorded_ts) if isinstance(recorded_ts, str) else None,
                    "reasoning": _text(step.get("reasoning")),
                    "message": _text(step.get("message")),
                    "tool_calls": calls,
                    "observation": _text(step.get("observation")),
                }
            finding = _mapping(payload.get("finding"))
            if kind == "monitor.suspicion" or finding:
                card_id = str(
                    payload.get("card_id")
                    or payload.get("finding_id")
                    or finding.get("finding_id")
                    or finding.get("id")
                    or f"{run_id}:{kind}"
                )
                citations = _citations(payload)
                cards[card_id] = {
                    "card_id": card_id,
                    "summary": _text(payload.get("summary") or finding.get("summary") or kind),
                    "severity": _text(payload.get("severity") or finding.get("severity"))
                    or "unknown",
                    "status": event["status"],
                    "prefix_step": event.get("prefix_step"),
                    "step_citations": citations,
                    "event_id": event["event_id"],
                    "seq": event["seq"],
                }
                if kind == "monitor.suspicion" and isinstance(payload.get("rule"), str):
                    tripwires[payload["rule"]] = citations
            if kind == "monitor.prefix_scored":
                prefix = event.get("prefix_step")
                prefixes.append(
                    {
                        "prefix_step": prefix,
                        "seq": event["seq"],
                        "event_id": event["event_id"],
                        "event_type": kind,
                        "status": event["status"],
                        "assessment": _text(payload.get("assessment")),
                        # The live worker emits trigger_count, not a probability score.
                        "score": None,
                        "tripwires": [
                            rule
                            for rule, citations in tripwires.items()
                            if any(
                                isinstance(s, int) and isinstance(prefix, int) and s <= prefix
                                for s in citations
                            )
                        ],
                    }
                )
            if kind == "monitor.gate_decided":
                gates.append(
                    {
                        "event_id": event["event_id"],
                        "seq": event["seq"],
                        "step_id": event.get("prefix_step"),
                        "decision": "allow" if payload.get("allow") is True else "block",
                        "status": event["status"],
                    }
                )
            if kind.endswith(_TERMINAL):
                terminal = kind.rsplit(".", 1)[-1]
                if terminal not in {"stopped", "cancelled", "canceled"}:
                    run_state = terminal
        return self._response(
            {
                "run_id": run_id,
                "dataset_id": item.dataset_id,
                "item_id": item.item_id,
                "split": item.split,
                "source_kind": source_kind,
                "run_state": run_state,
                "last_seq": events[-1]["seq"],
                "truncated": len(steps) > max_steps,
                "steps": list(steps.values())[-max_steps:],
                "prefixes": prefixes,
                "cards": list(cards.values()),
                "gates": gates,
                "controls": list(controls.values()),
            }
        )

    def _path(self, value: Any) -> Path | None:
        if not isinstance(value, (str, Path)) or not str(value):
            return None
        self._seal.check(str(value))
        path = resolve_within(self.allowed_roots, value)
        self._seal.check(str(path))
        return path

    def _tree(self, root: Path) -> dict[str, _File]:
        if not root.is_dir():
            raise LookupError(f"task package is not a directory: {root}")
        files: dict[str, _File] = {}
        pending = [(root, Path())]
        visited: set[Path] = set()
        while pending:
            directory, relative = pending.pop()
            if directory in visited:
                continue
            visited.add(directory)
            for child in sorted(directory.iterdir()):
                if child.name in _SKIP_DIRECTORIES:
                    continue
                self._seal.check(child.name)
                resolved = resolve_within(self.allowed_roots, child)
                self._seal.check(str(resolved))
                # Package links must not pull another task's files into a DEV tree.
                resolve_within((root,), resolved)
                name = relative / child.name
                if resolved.is_dir():
                    pending.append((resolved, name))
                elif resolved.is_file():
                    with resolved.open("rb") as stream:
                        sample = stream.read(_FILE_LIMIT)
                        digest = hashlib.sha256(sample)
                        while chunk := stream.read(_FILE_LIMIT):
                            digest.update(chunk)
                    binary = b"\0" in sample
                    try:
                        codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
                    except UnicodeDecodeError:
                        binary = True
                    files[name.as_posix()] = _File(resolved, digest.hexdigest(), binary)
        return files

    def _compare(self, original: Path, repaired: Path) -> list[dict[str, Any]]:
        before, after = self._tree(original), self._tree(repaired)
        files: list[dict[str, Any]] = []
        for name in before.keys() | after.keys():
            left, right = before.get(name), after.get(name)
            status = (
                "added"
                if left is None
                else "removed"
                if right is None
                else "unchanged"
                if left.digest == right.digest
                else "modified"
            )
            bodies: list[str | None] = []
            truncated = False
            for file in (left, right):
                if file is None or file.binary or status == "unchanged":
                    bodies.append(None)
                else:
                    text, cut = read_bounded_text(
                        resolve_within(self.allowed_roots, file.path), _FILE_LIMIT
                    )
                    bounded = _text(text, _FILE_LIMIT) or ""
                    bodies.append(bounded)
                    truncated |= cut or bounded != text
            binary = any(file.binary for file in (left, right) if file is not None)
            diff = None
            if status != "unchanged" and not binary:
                full_diff = "".join(
                    unified_diff(
                        (bodies[0] or "").splitlines(keepends=True),
                        (bodies[1] or "").splitlines(keepends=True),
                        fromfile=f"original/{name}",
                        tofile=f"repaired/{name}",
                    )
                )
                diff = _text(full_diff, _FILE_LIMIT)
                truncated |= diff != full_diff
            files.append(
                {
                    "path": name,
                    "status": status,
                    "original": bodies[0],
                    "repaired": bodies[1],
                    "unified_diff": diff,
                    "binary": binary,
                    "truncated": truncated,
                }
            )
        rank = {"modified": 0, "added": 1, "removed": 1, "unchanged": 2}
        return sorted(
            files,
            key=lambda file: (
                rank[file["status"]],
                not (
                    "tests" in Path(file["path"]).parts
                    or "verifier" in file["path"].lower()
                    or "grader" in file["path"].lower()
                ),
                file["path"],
            ),
        )

    @staticmethod
    def _has_trace_label(events: Sequence[Mapping[str, Any]]) -> bool:
        has_steps = has_label = False
        for event in events:
            payload, kind = _payload(event), str(event["event_type"])
            has_steps |= any(
                isinstance(step, Mapping)
                and _step_id(step.get("step_id", step.get("id"))) is not None
                for step in _raw_steps(payload, kind=kind)
            )
            if event["status"] != "confirmed" or kind not in {
                "monitor.suspicion",
                "seeker.exploit_confirmed",
                "monitor.gate_decided",
            }:
                continue
            has_label |= bool(_citations(payload)) or kind != "monitor.suspicion"
        return has_steps and has_label

    def grader_diff(
        self,
        *,
        run_id: str | None = None,
        asset_id: str | None = None,
        dataset_id: str | None = None,
        item_id: str | None = None,
    ) -> dict[str, Any]:
        """Compare one item's task trees after validating all effective DEV lineage."""
        selector = self._selector(dataset_id, item_id)
        if selector is not None and (asset_id is not None or run_id is None):
            raise ValueError("task selectors require run_id and cannot be combined with asset_id")
        self._seal.check({"run_id": run_id, "asset_id": asset_id})
        selected: dict[str, Any] | None = None
        if asset_id is not None:
            for event in self._events():
                payload = _payload(event)
                identity = payload.get("asset_id") or f"{payload.get('kind')}:{event['event_id']}"
                if (
                    event["event_type"] == "asset.verified"
                    and event["status"] in {"confirmed", "applied"}
                    and identity == asset_id
                ):
                    selected = event
            if selected is None:
                if asset_id.startswith("labeled-trace:"):
                    derived_run = asset_id.removeprefix("labeled-trace:")
                    if run_id is not None and run_id != derived_run:
                        raise LookupError("asset does not belong to the requested run")
                    run_id = derived_run
                else:
                    raise LookupError(asset_id)
            elif run_id is not None and run_id != selected["run_id"]:
                raise LookupError("asset does not belong to the requested run")
            else:
                run_id = str(selected["run_id"])
        if run_id is None:
            raise LookupError("run_id or asset_id is required")
        events = list(self._events(run_id))
        if not events:
            raise LookupError(run_id)
        events, item = self._select_events(events, selector, asset=selected)
        if asset_id is not None and selected is None and not self._has_trace_label(events):
            raise LookupError(asset_id)
        if selected is not None:
            events = [event for event in events if event["seq"] <= selected["seq"]]
        original: dict[str, Any] = {
            "task_package": None,
            "digest": None,
            "reward": None,
            "trial_dir": None,
        }
        repaired = dict(original)
        control_rewards: dict[str, list[float | None]] | None = None
        mechanism: str | None = None
        taxonomy: str | None = None
        exploit_files: dict[str, str] = {}
        bundle: Path | None = None
        for event in events:
            kind, payload = str(event["event_type"]), _payload(event)
            if kind not in {
                "asset.verified",
                "seeker.exploit_confirmed",
                "seeker.replay_observation",
            } and not kind.startswith("task_quality.repair_"):
                continue
            is_repaired = (
                payload.get("kind") == "repaired_task" or "original_task_package" in payload
            )
            original_value = (
                payload.get("original_task")
                or payload.get("original_task_package")
                or (payload.get("task_package") if not is_repaired else None)
            )
            repaired_value = (
                payload.get("repaired_task_materialized")
                or payload.get("repaired_task")
                or payload.get("fixed_task")
                or (payload.get("task_package") if is_repaired else None)
            )
            for side, value, digest in (
                (
                    original,
                    original_value,
                    payload.get("original_task_digest")
                    or (payload.get("task_package_digest") if not is_repaired else None),
                ),
                (
                    repaired,
                    repaired_value,
                    payload.get("repaired_task_digest")
                    or (payload.get("task_package_digest") if is_repaired else None),
                ),
            ):
                if value is not None:
                    path = self._path(value)
                    if path is not None:
                        if side["task_package"] != path:
                            side["digest"] = None
                        side["task_package"] = path
                        if digest is not None:
                            side["digest"] = _text(digest)
            for side, reward, trial in (
                (
                    original,
                    payload.get("original_reward", payload.get("upstream_reward")),
                    payload.get("original_trial_dir")
                    or payload.get("exploit_run")
                    or next(iter(payload.get("passing_runs") or []), None),
                ),
                (
                    repaired,
                    payload.get("repaired_reward"),
                    payload.get("repaired_trial_dir")
                    or payload.get("repaired_run")
                    or next(iter(payload.get("repaired_runs") or []), None),
                ),
            ):
                if _number(reward) is not None:
                    side["reward"] = _number(reward)
                if trial is not None:
                    side["trial_dir"] = str(self._path(trial))
            rewards = payload.get("control_rewards")
            if isinstance(rewards, Mapping):
                control_rewards = {
                    name: [_number(value) for value in rewards.get(name, [])]
                    for name in ("reference", "nop", "exploit")
                }
            elif kind == "task_quality.repair_control" and payload.get("control") in {
                "reference",
                "nop",
                "exploit",
            }:
                if control_rewards is None:
                    control_rewards = {name: [] for name in ("reference", "nop", "exploit")}
                control_rewards[payload["control"]].append(_number(payload.get("reward")))
            finding = _mapping(payload.get("finding"))
            mechanism = _text(payload.get("mechanism") or finding.get("mechanism")) or mechanism
            taxonomy = (
                _text(payload.get("taxonomy_class") or finding.get("taxonomy_class")) or taxonomy
            )
            if isinstance(payload.get("exploit_files"), Mapping):
                exploit_files = {
                    str(name): _text(text, _FILE_LIMIT) or ""
                    for name, text in payload["exploit_files"].items()
                }
            if payload.get("solution_bundle"):
                bundle = self._path(payload["solution_bundle"])
        if not isinstance(original["task_package"], Path) or not isinstance(
            repaired["task_package"], Path
        ):
            raise LookupError("original and repaired task packages are not recorded for this run")
        if bundle is not None and not exploit_files:
            for name, file in self._tree(bundle).items():
                if not file.binary:
                    text = read_bounded_text(
                        resolve_within(self.allowed_roots, file.path), _FILE_LIMIT
                    )[0]
                    exploit_files[name] = _text(text, _FILE_LIMIT) or ""
        files = self._compare(original["task_package"], repaired["task_package"])
        original["task_package"], repaired["task_package"] = (
            str(original["task_package"]),
            str(repaired["task_package"]),
        )
        return self._response(
            {
                "run_id": run_id,
                "asset_id": asset_id,
                "dataset_id": item.dataset_id,
                "item_id": item.item_id,
                "split": "dev",
                "original": original,
                "repaired": repaired,
                "control_rewards": control_rewards,
                "exploit": {
                    "mechanism": mechanism,
                    "taxonomy_class": taxonomy,
                    "files": exploit_files,
                }
                if mechanism or taxonomy or exploit_files
                else None,
                "files": files,
            }
        )
