"""Journal-derived verified assets and fail-closed, local DEV-only exports.

The in-memory index is disposable: a new store replays the ordered journal, and
successful exports are recorded there. File bytes are captured and checked before
creating any export directory; those same bytes, not a second filesystem read,
are written to an atomic, content-addressed export.
"""

from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
from collections import Counter, deque
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, NoReturn
from uuid import NAMESPACE_URL, uuid5

from evallab.registry import task_directory_digest

from .corpus_queue import CorpusRegistry, IdempotencyConflict
from .safe_paths import resolve_within

if TYPE_CHECKING:
    from .control_plane import EventJournal

_KINDS = ("repaired_task", "exploit_witness", "labeled_trace")
_PAGE_SIZE = 500
_MAX_BYTES = 50 * 1024 * 1024
_BUNDLE_FILE_BYTES = 64 * 1024
_PREVIEW_CHARS = 16 * 1024
_LABEL_EVENTS = frozenset({"monitor.suspicion", "seeker.exploit_confirmed", "monitor.gate_decided"})
_TOKEN = re.compile(r"[\w-]+")


class AssetNotFound(KeyError):
    """No verified asset with this identity exists in the journal."""


class ExportConflict(ValueError):
    """A request_id is bound to another export, or its export is still in progress."""


class ExportDisabled(RuntimeError):
    """The operator has not configured an export directory."""


class SplitLeakError(PermissionError):
    """An export contains sealed identifiers or lacks verifiable DEV lineage."""

    def __init__(self, matches: list[dict[str, str]]) -> None:
        super().__init__("verified asset export blocked by the TEST split-leak guard")
        self.matches = copy.deepcopy(matches)


@dataclass(frozen=True)
class _Asset:
    asset_id: str
    kind: str
    run_id: str
    event_id: str
    verified_at: str
    payload: dict[str, Any]
    source_event_ids: tuple[str, ...]


@dataclass
class _Context:
    primary: tuple[str, str] | None
    lineage: list[tuple[str, str | None, str | None]]
    payloads: list[tuple[str, dict[str, Any]]]
    merged: dict[str, Any]
    source_event_ids: list[str]


@dataclass
class _ExportRequest:
    asset_id: str
    operator: str
    record: dict[str, Any] | None = None


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")


def _walk_error(error: OSError) -> NoReturn:
    raise error


def _read_regular(path: Path, limit: int) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise ValueError("asset file cannot be opened safely") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("asset entry is not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read(limit + 1)
    finally:
        os.close(descriptor)


def _identifier(value: Any) -> str | int | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    if isinstance(value, str):
        if not value.strip():
            return None
        match = re.fullmatch(r"head#(\d+)", value)
        if match:
            return int(match[1])
    return value


def _step_key(value: str | int) -> tuple[int, int, str]:
    text = str(value)
    return (0, int(text), "") if text.isdecimal() else (1, 0, text)


def _safe_name(value: str) -> str:
    # Reject rather than normalize: a/../b and Windows separators are never
    # allowed to change the destination or collide with another supplied name.
    parts = value.split("/")
    if (
        not value
        or PurePosixPath(value).is_absolute()
        or "\\" in value
        or ":" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(part in {"", ".", ".."} or len(part.encode("utf-8")) > 255 for part in parts)
    ):
        raise ValueError(f"unsafe asset file name: {value!r}")
    return value


def _references(payload: Mapping[str, Any]) -> Iterator[str]:
    for key in ("qualification_event_id", "source_event_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            yield value
    for key in ("source_event_ids", "provenance_event_ids"):
        values = payload.get(key)
        if isinstance(values, list):
            yield from (value for value in values if isinstance(value, str) and value)


def _lineage(value: Any, field: str) -> Iterator[tuple[str, str | None, str | None]]:
    if isinstance(value, dict):
        if "dataset_id" in value or "item_id" in value:
            dataset, item = value.get("dataset_id"), value.get("item_id")
            yield (
                field,
                dataset if isinstance(dataset, str) and dataset else None,
                item if isinstance(item, str) and item else None,
            )
        for key, child in value.items():
            yield from _lineage(child, f"{field}.{key}")
    elif isinstance(value, list):
        for number, child in enumerate(value):
            yield from _lineage(child, f"{field}[{number}]")


class VerifiedAssetStore:
    """Thread-safe, incremental asset projection over the real production journal."""

    def __init__(
        self,
        journal: EventJournal,
        registry: CorpusRegistry,
        *,
        export_root: Path | None,
        allowed_roots: Sequence[Path],
    ) -> None:
        self.journal, self.registry = journal, registry
        self.export_root = None if export_root is None else export_root.resolve()
        self.allowed_roots = tuple(root.resolve() for root in allowed_roots)
        self._lock = threading.RLock()
        self._cursor = 0
        self._events: dict[str, dict[str, Any]] = {}
        self._runs: dict[str, list[str]] = {}
        self._assets: dict[str, _Asset] = {}
        self._steps: dict[str, dict[str, dict[str, Any]]] = {}
        self._step_events: dict[str, set[str]] = {}
        self._label_events: dict[str, list[str]] = {}
        self._exports: dict[str, dict[str, str]] = {}
        self._descriptors: dict[str, dict[str, Any]] = {}
        self._dependents: dict[str, set[str]] = {}
        self._requests: dict[str, _ExportRequest] = {}
        sealed: set[tuple[str, str]] = set()
        for item in registry.items(split="test"):
            sealed.add((item.item_id, "test_split_item"))
            if item.task_id:
                sealed.add((item.task_id, "sealed_task_name"))
                sealed.add((item.task_id.split("@", 1)[0].rsplit("/", 1)[-1], "sealed_task_name"))
            if item.task_digest:
                sealed.add((item.task_digest, "sealed_digest"))
                sealed.add((item.task_digest.removeprefix("sha256:"), "sealed_digest"))
        self._sealed: tuple[
            dict[str, list[tuple[str, str, int]]], dict[str, list[tuple[str, str, int]]]
        ] = ({}, {})
        self._punctuation: list[tuple[str, str, re.Pattern[str]]] = []
        for token, reason in sorted(sealed):
            if not token:
                continue
            pieces = list(_TOKEN.finditer(token))
            if pieces:
                # The longest word is an exact dictionary anchor. Even a very
                # large TEST index costs one lexical pass over each text, not
                # one complete regex search for every sealed identifier.
                anchor = max(pieces, key=lambda piece: len(piece[0]))
                insensitive = reason == "sealed_digest"
                key = anchor[0].casefold() if insensitive else anchor[0]
                self._sealed[int(insensitive)].setdefault(key, []).append(
                    (token, reason, anchor.start())
                )
            else:
                self._punctuation.append(
                    (token, reason, re.compile(r"(?<![\w-])" + re.escape(token) + r"(?![\w-])"))
                )
        self._anchor_lengths = tuple(frozenset(map(len, index)) for index in self._sealed)

    def _refresh(self) -> None:
        dirty_runs: set[str] = set()
        trace_runs: set[str] = set()
        snapshot_reader = getattr(self.journal, "queue_snapshot", None)
        snapshot = snapshot_reader() if callable(snapshot_reader) else {}
        high_water = snapshot.get("last_seq")
        events: list[dict[str, Any]] | None = None
        if not isinstance(high_water, int):
            events = self.journal.read_events(after_seq=self._cursor, limit=_PAGE_SIZE)
            high_water = max((int(event["seq"]) for event in events), default=self._cursor)
        while self._cursor < high_water:
            limit = min(_PAGE_SIZE, high_water - self._cursor)
            if events is None:
                events = self.journal.read_events(after_seq=self._cursor, limit=limit)
            for event in events:
                seq = int(event["seq"])
                if seq > high_water:
                    break
                if seq <= self._cursor:
                    continue
                event_id, run_id = str(event["event_id"]), str(event["run_id"])
                self._events[event_id] = event
                self._runs.setdefault(run_id, []).append(event_id)
                dirty_runs.add(run_id)
                for asset_id in self._dependents.get(event_id, ()):
                    self._descriptors.pop(asset_id, None)
                payload, kind = event["payload"], event["event_type"]
                if kind == "asset.verified" and event["status"] in {"confirmed", "applied"}:
                    asset_kind = payload.get("kind")
                    if asset_kind in {"repaired_task", "exploit_witness"}:
                        asset_id = str(payload.get("asset_id") or f"{asset_kind}:{event_id}")
                        self._assets[asset_id] = _Asset(
                            asset_id,
                            asset_kind,
                            run_id,
                            event_id,
                            str(event["ts"]),
                            payload,
                            (event_id,),
                        )
                if kind == "asset.exported":
                    asset_id = payload.get("asset_id")
                    if (
                        event["status"] == "applied"
                        and isinstance(asset_id, str)
                        and isinstance(payload.get("export_path"), str)
                        and isinstance(payload.get("exported_at"), str)
                    ):
                        self._exports[asset_id] = {
                            "export_path": payload["export_path"],
                            "exported_at": payload["exported_at"],
                        }
                        request_id, operator = payload.get("request_id"), payload.get("operator")
                        if isinstance(request_id, str) and isinstance(operator, str):
                            binding = self._requests.setdefault(
                                request_id, _ExportRequest(asset_id, operator)
                            )
                            if (
                                binding.asset_id == asset_id
                                and binding.operator == operator
                                and binding.record is None
                            ):
                                binding.record = {**payload, "run_id": run_id}
                steps = payload.get("steps")
                if not isinstance(steps, list):
                    step = payload.get("step")
                    steps = [step] if isinstance(step, dict) else []
                    if not steps and kind == "trace.step" and "step_id" in payload:
                        steps = [payload]
                # A seeker's attempt/feedback "step" is phase telemetry, not a rollout.
                if kind in {"seeker.attempt_started", "seeker.grader_feedback"}:
                    steps = []
                for step in steps:
                    if not isinstance(step, dict):
                        continue
                    step_id = _identifier(step.get("step_id", step.get("id")))
                    if step_id is None:
                        continue
                    indexed = self._steps.setdefault(run_id, {})
                    previous = indexed.get(str(step_id), {})
                    indexed[str(step_id)] = {
                        **previous,
                        **step,
                        "step_id": step_id,
                        "source_event_id": event_id,
                    }
                    self._step_events.setdefault(run_id, set()).add(event_id)
                    trace_runs.add(run_id)
                if kind in _LABEL_EVENTS and event["status"] == "confirmed":
                    self._label_events.setdefault(run_id, []).append(event_id)
                    trace_runs.add(run_id)
                self._cursor = seq
            if len(events) < limit or not events or self._cursor >= high_water:
                break
            events = None
        for run_id in trace_runs:
            self._derive_trace(run_id)
        if dirty_runs:
            for asset_id, asset in self._assets.items():
                if asset.run_id in dirty_runs:
                    self._descriptors.pop(asset_id, None)

    def _derive_trace(self, run_id: str) -> None:
        steps = sorted(
            self._steps.get(run_id, {}).values(), key=lambda step: _step_key(step["step_id"])
        )
        if not steps:
            return
        labels: list[dict[str, Any]] = []
        label_ids: set[str] = set()
        for event_id in self._label_events.get(run_id, []):
            event = self._events[event_id]
            payload, kind = event["payload"], event["event_type"]
            citations: dict[str, str | int] = {}
            finding = payload.get("finding")
            containers = [payload, finding] if isinstance(finding, dict) else [payload]
            for container in containers:
                for key in ("step_citations", "evidence"):
                    values = container.get(key)
                    if not isinstance(values, list):
                        continue
                    for value in values:
                        if isinstance(value, dict):
                            value = value.get("step_id", value.get("step", value.get("step_ref")))
                        step_id = _identifier(value)
                        if step_id is not None:
                            citations[str(step_id)] = step_id
            if kind != "monitor.suspicion" and not citations:
                step_id = _identifier(payload.get("step_id", event.get("prefix_step")))
                if step_id is None:
                    # Run-level outcomes attach to the last observed step. A
                    # recorded replay may append its trace after the outcome.
                    candidates = [
                        step
                        for step in steps
                        if int(self._events[step["source_event_id"]]["seq"]) <= int(event["seq"])
                    ]
                    step_id = (candidates or steps)[-1]["step_id"]
                if step_id is not None:
                    citations[str(step_id)] = step_id
            if not citations:
                continue
            label = (
                payload.get("judge_label")
                or payload.get("label")
                or payload.get("rule")
                or payload.get("decision")
            )
            if not isinstance(label, str) or not label:
                if kind == "monitor.gate_decided":
                    allow = payload.get("allow")
                    label = (
                        "allow" if allow is True else "block" if allow is False else "gate_decided"
                    )
                else:
                    label = kind.removeprefix("seeker.")
            for step_id in sorted(citations.values(), key=_step_key):
                labels.append(
                    {
                        "step_id": step_id,
                        "label": label,
                        "source_event_id": event_id,
                        "status": "confirmed",
                    }
                )
            label_ids.add(event_id)
        if not labels:
            return
        cutoff = min((label["step_id"] for label in labels), key=_step_key)
        sources = self._step_events.get(run_id, set()) | label_ids
        source_ids = tuple(sorted(sources, key=lambda value: int(self._events[value]["seq"])))
        last_label = max(
            (self._events[value] for value in label_ids), key=lambda event: event["seq"]
        )
        asset_id = f"labeled-trace:{run_id}"
        self._assets[asset_id] = _Asset(
            asset_id,
            "labeled_trace",
            run_id,
            last_label["event_id"],
            last_label["ts"],
            {
                "asset_id": asset_id,
                "kind": "labeled_trace",
                "run_id": run_id,
                "steps": steps,
                "labels": labels,
                "cutoff_step": cutoff,
                "causal_prefix_step_ids": [
                    step["step_id"]
                    for step in steps
                    if _step_key(step["step_id"]) < _step_key(cutoff)
                ],
                "provenance_event_ids": list(source_ids),
            },
            source_ids,
        )

    def _asset(self, asset_id: str) -> _Asset:
        try:
            return self._assets[asset_id]
        except KeyError:
            for asset in self._assets.values():
                if self._opaque_id(asset) == asset_id:
                    return asset
            raise AssetNotFound(asset_id) from None

    def _context(self, asset: _Asset) -> _Context:
        payloads = [("payload", asset.payload)]
        sources: dict[str, dict[str, Any]] = {}
        pending = deque((*asset.source_event_ids, *_references(asset.payload)))
        while pending:
            event_id = pending.popleft()
            self._dependents.setdefault(event_id, set()).add(asset.asset_id)
            if event_id in sources:
                continue
            event = self._events.get(event_id) or self.journal.get_event(event_id)
            if event is None:
                continue
            sources[event_id] = event
            if event["payload"] is not asset.payload:
                payloads.append((f"events.{event_id}.payload", event["payload"]))
            pending.extend(value for value in _references(event["payload"]) if value not in sources)
        lineage = [pair for field, payload in payloads for pair in _lineage(payload, field)]
        primary = next(((ds, item) for _, ds, item in lineage if ds and item), None)
        # Run lineage is also checked when the asset supplies its own pair: an
        # explicit DEV claim must not mask a sealed upstream task in that run.
        for event_id in self._runs.get(asset.run_id, []):
            event = self._events[event_id]
            if event["event_type"] == "asset.exported":
                continue
            pairs = list(_lineage(event["payload"], f"events.{event_id}.payload"))
            lineage.extend(pairs)
            if primary is None:
                primary = next(((ds, item) for _, ds, item in pairs if ds and item), None)
                if primary is not None:
                    sources[event_id] = event
        merged: dict[str, Any] = {}
        for _, payload in reversed(payloads):
            merged.update(payload)
        return _Context(
            primary,
            lineage,
            payloads,
            merged,
            sorted(sources, key=lambda value: int(sources[value]["seq"])),
        )

    def _scan(self, value: Any, field: str, matches: list[dict[str, str]]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                self._scan(str(key), field, matches)
                self._scan(child, field, matches)
        elif isinstance(value, list):
            for child in value:
                self._scan(child, field, matches)
        elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
            text = str(value)
            found: set[tuple[str, str]] = set()
            for word in _TOKEN.finditer(text):
                for insensitive, index in enumerate(self._sealed):
                    if word.end() - word.start() not in self._anchor_lengths[insensitive]:
                        continue
                    key = word[0].casefold() if insensitive else word[0]
                    for token, reason, offset in index.get(key, ()):
                        start = word.start() - offset
                        end = start + len(token)
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
                            found.add((token, reason))
            for token, reason, pattern in self._punctuation:
                if pattern.search(text):
                    found.add((token, reason))
            for _token, reason in sorted(found):
                match = {"field": field, "reason": reason}
                if match not in matches:
                    matches.append(match)

    def _base_guard(self, asset: _Asset, context: _Context) -> dict[str, Any]:
        matches: list[dict[str, str]] = []
        if context.primary is None:
            matches.append({"field": "lineage", "reason": "lineage_unresolved"})
        checked_pairs: set[tuple[str | None, str | None]] = set()
        for _field, dataset, item_id in context.lineage:
            pair = dataset, item_id
            if pair in checked_pairs:
                continue
            checked_pairs.add(pair)
            try:
                resolved = self.registry.get(dataset, item_id) if dataset and item_id else None
            except KeyError:
                resolved = None
            if resolved is None:
                matches.append(
                    {
                        "field": "lineage",
                        "reason": "lineage_unresolved",
                    }
                )
            elif resolved.split != "dev":
                matches.append(
                    {
                        "field": "lineage",
                        "reason": "test_split_item",
                    }
                )
        for _, payload in context.payloads:
            self._scan(payload, "payload", matches)
        self._scan(
            {
                "asset_id": asset.asset_id,
                "run_id": asset.run_id,
                "source_event_ids": context.source_event_ids,
            },
            "manifest",
            matches,
        )
        if self.export_root is not None:
            self._scan(str(self.export_root), "destination", matches)
        return {
            "result": "blocked" if matches else "pass",
            "checked": ["effective_split", "asset_payload", "manifest"],
            "matches": matches,
        }

    @staticmethod
    def _opaque_id(asset: _Asset) -> str:
        return "blocked-asset:" + hashlib.sha256(asset.asset_id.encode("utf-8")).hexdigest()

    def _redacted(self, asset: _Asset, reason: str) -> dict[str, Any]:
        descriptor = {
            "asset_id": self._opaque_id(asset),
            "kind": asset.kind,
            "exportable": False,
            "blocked_reason": reason,
        }
        self._descriptors[asset.asset_id] = descriptor
        return descriptor

    def _descriptor(self, asset: _Asset) -> dict[str, Any]:
        cached = self._descriptors.get(asset.asset_id)
        if cached is not None:
            return cached
        try:
            context, _, guarded = self._prepare(asset)
        except PermissionError:
            return self._redacted(asset, "path_outside_roots")
        except (OSError, ValueError):
            return self._redacted(asset, "asset_unavailable")
        self._scan(self._exports.get(asset.asset_id), "manifest", guarded["matches"])
        if guarded["matches"]:
            return self._redacted(asset, guarded["matches"][0]["reason"])
        dataset, item_id = context.primary or (None, None)
        try:
            split = self.registry.get(dataset, item_id).split if dataset and item_id else None
        except KeyError:
            split = None
        finding = asset.payload.get("finding")
        summary = asset.payload.get("summary")
        if not isinstance(summary, str) and isinstance(finding, dict):
            summary = finding.get("summary")
        if not isinstance(summary, str):
            summary = f"{asset.kind.replace('_', ' ')} · {asset.run_id}"
        blocked_reason = "export_disabled" if self.export_root is None else None
        descriptor = {
            "asset_id": asset.asset_id,
            "kind": asset.kind,
            "run_id": asset.run_id,
            "dataset_id": dataset,
            "item_id": item_id,
            "split": split,
            "event_id": asset.event_id,
            "verified_at": asset.verified_at,
            "summary": summary[:1000],
            "exportable": blocked_reason is None,
            "blocked_reason": blocked_reason,
            "exported": self._exports.get(asset.asset_id),
        }
        self._descriptors[asset.asset_id] = descriptor
        return descriptor

    def list_assets(self) -> dict[str, Any]:
        with self._lock:
            self._refresh()
            assets = [self._descriptor(asset) for asset in self._assets.values()]
            counts = Counter(asset["kind"] for asset in assets)
            return copy.deepcopy(
                {
                    "assets": assets,
                    "by_kind": {kind: counts[kind] for kind in _KINDS},
                    "total": len(assets),
                }
            )

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        with self._lock:
            self._refresh()
            asset = self._asset(asset_id)
            self._descriptors.pop(asset.asset_id, None)
            descriptor = copy.deepcopy(self._descriptor(asset))
            if "run_id" not in descriptor:
                return descriptor
            return {
                **descriptor,
                "preview": _json_bytes(asset.payload).decode("utf-8")[:_PREVIEW_CHARS],
            }

    def _read_tree(self, root: Path, *, bounded: bool) -> tuple[dict[str, bytes], list[str]]:
        if not root.is_dir():
            raise ValueError(f"asset directory is missing: {root}")
        files: dict[str, bytes] = {}
        truncated: list[str] = []
        size = 0
        for directory, names, filenames in os.walk(root, followlinks=False, onerror=_walk_error):
            parent = Path(directory)
            names[:] = sorted(name for name in names if not (parent / name).is_symlink())
            for filename in sorted(filenames):
                source = parent / filename
                if source.is_symlink():
                    continue
                source = resolve_within(self.allowed_roots, source)
                if not source.is_file():
                    raise ValueError(f"asset entry is not a regular file: {source}")
                relative = _safe_name(source.relative_to(root).as_posix())
                limit = min(_BUNDLE_FILE_BYTES, _MAX_BYTES - size) if bounded else _MAX_BYTES - size
                if not bounded and source.stat().st_size > limit:
                    raise ValueError("asset exceeds the 50 MiB export size limit")
                content = _read_regular(source, limit)
                if len(content) > limit:
                    if not bounded or limit < _BUNDLE_FILE_BYTES:
                        raise ValueError("asset exceeds the 50 MiB export size limit")
                    truncated.append(relative)
                    content = content[:limit]
                files[relative] = content
                size += len(content)
        return files, truncated

    def _files(self, asset: _Asset, context: _Context) -> dict[str, bytes]:
        payload = context.merged
        files: dict[str, bytes] = {}
        if asset.kind == "repaired_task":
            value = payload.get("task_package") or payload.get("repaired_task")
            if not isinstance(value, str):
                raise ValueError("repaired task has no task package path")
            package = resolve_within(self.allowed_roots, value)
            contents, _ = self._read_tree(package, bounded=False)
            files.update((f"task_package/{name}", content) for name, content in contents.items())
            files["qualification.json"] = _json_bytes(
                {
                    "qualification_event_id": payload.get("qualification_event_id"),
                    "control_rewards": payload.get("control_rewards"),
                    "control_evidence": payload.get("control_evidence"),
                    "source_event_ids": context.source_event_ids,
                }
            )
        elif asset.kind == "exploit_witness":
            supplied = payload.get("exploit_files", {})
            if not isinstance(supplied, dict):
                raise ValueError("exploit_files must be a file-name to text mapping")
            for name, content in supplied.items():
                if not isinstance(name, str) or not isinstance(content, str):
                    raise ValueError("exploit_files must contain only text files")
                files[f"exploit_files/{_safe_name(name)}"] = content.encode("utf-8")
            truncated: list[str] = []
            bundle = payload.get("solution_bundle")
            if bundle is not None:
                if not isinstance(bundle, str):
                    raise ValueError("solution bundle path must be text")
                contents, truncated = self._read_tree(
                    resolve_within(self.allowed_roots, bundle), bounded=True
                )
                files.update(
                    (f"solution_bundle/{name}", content) for name, content in contents.items()
                )
            files["witness.json"] = _json_bytes(
                {
                    "original_reward": payload.get(
                        "original_reward", payload.get("upstream_reward")
                    ),
                    "repaired_reward": payload.get("repaired_reward"),
                    "candidate_digest": payload.get("candidate_digest"),
                    "shortcut_signature": payload.get("shortcut_signature"),
                    "mechanism": payload.get("mechanism"),
                    "taxonomy_class": payload.get("taxonomy_class"),
                    "finding": payload.get("finding"),
                    "exploit_run": payload.get("exploit_run"),
                    "passing_runs": payload.get("passing_runs"),
                    "failing_runs": payload.get("failing_runs"),
                    "repaired_runs": payload.get(
                        "repaired_runs",
                        [payload["repaired_run"]] if payload.get("repaired_run") else None,
                    ),
                    "source_event_ids": context.source_event_ids,
                    "solution_bundle_truncated": truncated,
                }
            )
        else:
            files["trace.jsonl"] = b"".join(
                (
                    json.dumps(step, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
                ).encode("utf-8")
                for step in asset.payload["steps"]
            )
            files["labels.json"] = _json_bytes(
                {
                    "asset_id": asset.asset_id,
                    "run_id": asset.run_id,
                    "labels": asset.payload["labels"],
                    "cutoff_step": asset.payload["cutoff_step"],
                    "causal_prefix_step_ids": asset.payload["causal_prefix_step_ids"],
                    "provenance_event_ids": context.source_event_ids,
                }
            )
        if sum(len(content) for content in files.values()) > _MAX_BYTES:
            raise ValueError("asset exceeds the 50 MiB export size limit")
        for name in files:
            _safe_name(name)
            if any(
                parent.as_posix() in files
                for parent in PurePosixPath(name).parents
                if parent.as_posix() != "."
            ):
                raise ValueError("asset file names collide with a directory")
        return files

    def _prepare(self, asset: _Asset) -> tuple[_Context, dict[str, bytes], dict[str, Any]]:
        context = self._context(asset)
        guarded = self._base_guard(asset, context)
        if guarded["result"] == "blocked":
            return context, {}, guarded
        files = self._files(asset, context)
        for name, content in files.items():
            self._scan(name, "file_name", guarded["matches"])
            self._scan(
                content.decode("utf-8", errors="replace"), "file_content", guarded["matches"]
            )
        guarded["checked"].append("export_files")
        guarded["result"] = "blocked" if guarded["matches"] else "pass"
        return context, files, guarded

    def guard(self, asset_id: str) -> dict[str, Any]:
        with self._lock:
            self._refresh()
            asset = self._asset(asset_id)
            guarded = self._prepare(asset)[2]
            if guarded["matches"]:
                self._redacted(asset, guarded["matches"][0]["reason"])
            return guarded

    @staticmethod
    def _verify_package(directory: Path, expected: str | None) -> None:
        # Hash the guarded snapshot, not the mutable source tree. In particular,
        # the registry's digest helper must never follow a source symlink.
        if expected is not None and task_directory_digest(directory / "task_package") != expected:
            raise ValueError("repaired task package digest does not match its verified asset")

    def _read_manifest(self, destination: Path) -> tuple[dict[str, Any], str]:
        if destination.is_symlink() or not destination.is_dir():
            raise ValueError("existing asset export is not a regular directory")
        assert self.export_root is not None
        manifest_path = destination / "manifest.json"
        if manifest_path.is_symlink():
            raise ValueError("existing asset manifest must not be a symlink")
        resolve_within((self.export_root,), manifest_path)
        raw = _read_regular(manifest_path, _MAX_BYTES)
        if len(raw) > _MAX_BYTES:
            raise ValueError("existing asset manifest exceeds the size limit")
        matches: list[dict[str, str]] = []
        self._scan(raw.decode("utf-8", errors="replace"), "manifest", matches)
        if matches:
            raise SplitLeakError(matches)
        manifest = json.loads(raw)
        if not isinstance(manifest, dict):
            raise ValueError("existing asset manifest must be an object")
        split_guard = manifest.get("split_guard")
        if not isinstance(split_guard, dict) or split_guard.get("result") != "pass":
            raise ValueError("existing asset manifest has no passing split guard")
        return manifest, hashlib.sha256(raw).hexdigest()

    def _existing(
        self,
        destination: Path,
        metadata: dict[str, Any],
        entries: list[dict[str, Any]],
        package_digest: str | None,
        *,
        expected_sha256: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        manifest, manifest_sha256 = self._read_manifest(destination)
        if expected_sha256 is not None and expected_sha256 != manifest_sha256:
            raise ValueError("existing asset manifest failed its checksum")
        if (
            any(manifest.get(key) != value for key, value in metadata.items())
            or manifest.get("files") != entries
            or manifest.get("task_package_digest") != package_digest
        ):
            raise ValueError("existing content-addressed asset export has conflicting metadata")
        if not isinstance(entries, list):
            raise ValueError("existing asset manifest has invalid file records")
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("path"), str)
                or type(entry.get("bytes")) is not int
                or not 0 <= entry["bytes"] <= _MAX_BYTES
                or not isinstance(entry.get("sha256"), str)
            ):
                raise ValueError("existing asset manifest has invalid file records")
            _safe_name(entry["path"])
        if sum(entry["bytes"] for entry in entries) > _MAX_BYTES:
            raise ValueError("existing asset export exceeds the size limit")
        expected_names = {entry["path"] for entry in entries} | {"manifest.json"}
        actual_names: set[str] = set()
        for directory, names, filenames in os.walk(
            destination, followlinks=False, onerror=_walk_error
        ):
            if any((Path(directory) / name).is_symlink() for name in names + filenames):
                raise ValueError("existing asset export contains a symlink")
            actual_names.update(
                (Path(directory) / name).relative_to(destination).as_posix() for name in filenames
            )
        if actual_names != expected_names:
            raise ValueError("existing asset export contains unexpected or missing files")
        assert self.export_root is not None
        matches: list[dict[str, str]] = []
        for entry in entries:
            path = destination / entry["path"]
            resolve_within((self.export_root,), path)
            data = _read_regular(path, entry["bytes"])
            self._scan(entry["path"], "file_name", matches)
            self._scan(data.decode("utf-8", errors="replace"), "file_content", matches)
            if matches:
                raise SplitLeakError(matches)
            if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise ValueError("existing asset export failed its manifest checksum")
        return manifest, manifest_sha256

    @staticmethod
    def _result(
        destination: Path, manifest: dict[str, Any], manifest_sha256: str
    ) -> dict[str, Any]:
        return {
            "asset_id": manifest["asset_id"],
            "kind": manifest["kind"],
            "export_path": str(destination),
            "manifest": manifest,
            "manifest_sha256": manifest_sha256,
            "split_guard": manifest["split_guard"],
        }

    def _retry(self, binding: _ExportRequest) -> dict[str, Any]:
        assert binding.record is not None
        record = binding.record
        matches: list[dict[str, str]] = []
        self._scan(record, "manifest", matches)
        try:
            item = self.registry.get(record["dataset_id"], record["item_id"])
        except KeyError:
            matches.append({"field": "lineage", "reason": "lineage_unresolved"})
        else:
            if item.split != "dev":
                matches.append({"field": "lineage", "reason": "test_split_item"})
        if self.export_root is not None:
            self._scan(str(self.export_root), "destination", matches)
        if matches:
            raise SplitLeakError(matches)
        if self.export_root is None:
            raise ExportDisabled("verified asset export is disabled; configure an export root")
        destination = Path(record["export_path"])
        if destination.is_symlink():
            raise ValueError("asset export directory must not be a symlink")
        destination = resolve_within((self.export_root,), destination)
        self._scan(str(destination), "destination", matches)
        if matches:
            raise SplitLeakError(matches)
        manifest, _ = self._read_manifest(destination)
        metadata = {
            "schema": "evallab.verified-asset/v1",
            "asset_id": binding.asset_id,
            "kind": record["kind"],
            "dataset_id": record["dataset_id"],
            "item_id": record["item_id"],
            "split": "dev",
            "source_event_ids": manifest.get("source_event_ids"),
        }
        files = manifest.get("files")
        if not isinstance(files, list):
            raise ValueError("existing asset manifest has no file list")
        manifest, checksum = self._existing(
            destination,
            metadata,
            files,
            manifest.get("task_package_digest"),
            expected_sha256=record["manifest_sha256"],
        )
        return self._result(destination, manifest, checksum)

    def export(self, asset_id: str, *, operator: str, request_id: str) -> dict[str, Any]:
        with self._lock:
            self._refresh()
            asset = self._asset(asset_id)
            binding = self._requests.get(request_id)
            if binding is not None:
                if (binding.asset_id, binding.operator) != (asset.asset_id, operator):
                    raise ExportConflict("request_id already belongs to a different asset export")
                if binding.record is not None:
                    return self._retry(binding)
                raise ExportConflict("asset export request is already in progress")
            binding = _ExportRequest(asset.asset_id, operator)
            self._requests[request_id] = binding
            try:
                return self._export(
                    asset, operator=operator, request_id=request_id, binding=binding
                )
            finally:
                if binding.record is None:
                    self._requests.pop(request_id, None)

    def _export(
        self, asset: _Asset, *, operator: str, request_id: str, binding: _ExportRequest
    ) -> dict[str, Any]:
        context, files, guarded = self._prepare(asset)
        if guarded["result"] == "blocked":
            self._redacted(asset, guarded["matches"][0]["reason"])
            raise SplitLeakError(guarded["matches"])
        self._scan({"operator": operator, "request_id": request_id}, "manifest", guarded["matches"])
        if guarded["matches"]:
            raise SplitLeakError(guarded["matches"])
        if self.export_root is None:
            raise ExportDisabled("verified asset export is disabled; configure an export root")
        assert context.primary is not None
        dataset, item_id = context.primary
        metadata = {
            "schema": "evallab.verified-asset/v1",
            "asset_id": asset.asset_id,
            "kind": asset.kind,
            "dataset_id": dataset,
            "item_id": item_id,
            "split": "dev",
            "source_event_ids": context.source_event_ids,
        }
        entries = [
            {"path": name, "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
            for name, content in sorted(files.items())
        ]
        package_digest = (
            context.merged.get("task_package_digest") if asset.kind == "repaired_task" else None
        )
        if package_digest is not None and not isinstance(package_digest, str):
            raise ValueError("task_package_digest must be text")
        digest = hashlib.sha256(_json_bytes({**metadata, "files": entries})).hexdigest()
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", asset.asset_id).strip(".-_")[:96] or "asset"
        destination = self.export_root / asset.kind / f"{slug}-{digest[:12]}"
        if destination.is_symlink():
            raise ValueError("asset export directory must not be a symlink")
        destination = resolve_within((self.export_root,), destination)
        manifest = {
            **metadata,
            "operator": operator,
            "request_id": request_id,
            "exported_at": datetime.now(UTC).isoformat(),
            "task_package_digest": package_digest,
            "files": entries,
            "split_guard": guarded,
        }
        manifest_bytes = _json_bytes(manifest)
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        event_payload = {
            "asset_id": asset.asset_id,
            "kind": asset.kind,
            "dataset_id": dataset,
            "item_id": item_id,
            "source_event_ids": context.source_event_ids,
            "export_path": str(destination),
            "exported_at": manifest["exported_at"],
            "manifest_sha256": manifest_sha256,
            "content_sha256": digest,
            "operator": operator,
            "request_id": request_id,
        }
        # Check all bytes and the complete path before creating directories or
        # persisting the operation's binding to the journal.
        self._scan(manifest_bytes.decode("utf-8"), "manifest", guarded["matches"])
        self._scan(event_payload, "manifest", guarded["matches"])
        self._scan(str(destination), "destination", guarded["matches"])
        if guarded["matches"]:
            raise SplitLeakError(guarded["matches"])
        if destination.exists():
            manifest, manifest_sha256 = self._existing(
                destination, metadata, entries, package_digest
            )
        else:
            self.export_root.mkdir(parents=True, exist_ok=True)
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=".asset-", dir=self.export_root))
            try:
                for name, content in files.items():
                    path = staging / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
                self._verify_package(staging, package_digest)
                (staging / "manifest.json").write_bytes(manifest_bytes)
                try:
                    staging.rename(destination)
                except OSError as exc:
                    if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                        raise
                    manifest, manifest_sha256 = self._existing(
                        destination, metadata, entries, package_digest
                    )
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        event_payload.update(exported_at=manifest["exported_at"], manifest_sha256=manifest_sha256)
        self._scan(event_payload, "manifest", guarded["matches"])
        if guarded["matches"]:
            raise SplitLeakError(guarded["matches"])
        identity = (
            "evallab.production.asset-export:"
            + hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        )
        event_id = str(uuid5(NAMESPACE_URL, identity))
        try:
            event = self.journal.append_event(
                {
                    "event_id": event_id,
                    "run_id": asset.run_id,
                    "system": "queue",
                    "event_type": "asset.exported",
                    "ts": manifest["exported_at"],
                    "status": "applied",
                    "payload": event_payload,
                },
                idempotency_key=identity,
            )
        except IdempotencyConflict:
            original = self.journal.get_event(event_id)
            if original is None or (
                original["payload"].get("asset_id"),
                original["payload"].get("operator"),
            ) != (asset.asset_id, operator):
                raise ExportConflict(
                    "request_id already belongs to a different asset export"
                ) from None
            binding.record = {**original["payload"], "run_id": original["run_id"]}
            return self._retry(binding)
        binding.record = {**event["payload"], "run_id": asset.run_id}
        self._refresh()
        return self._result(destination, manifest, manifest_sha256)
