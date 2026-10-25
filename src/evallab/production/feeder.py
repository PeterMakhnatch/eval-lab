"""Replay retained DEV evidence into the real production journal and prefix monitor.

No model, sandbox or grader is launched. Historical steps/outcomes are explicitly
``public_replay``; prefix detections and executor control acknowledgements are
computed now. Pause, cancel and quarantine are terminal for a replayed run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import secrets
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from evallab.production.corpus_queue import (
    ContinuousWorkQueue,
    CorpusItem,
    CorpusRegistry,
    SplitLockError,
)
from evallab.production.monitor_worker import MisalignmentLiveWorker
from evallab.production.safe_paths import read_bounded_text, resolve_within
from evallab.registry import task_directory_digest

_DATASETS = {"tb4-dev": ("tb4-audit-v1",), "mimo": ("mimo-defect-map-v1",)}
_DATASETS["both"] = _DATASETS["tb4-dev"] + _DATASETS["mimo"]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key in replay provenance")
        result[key] = value
    return result


def _inventory_task(line: str, *, field_name: str = "task") -> str:
    """Decode only top-level keys and identity before deserializing a DEV row."""
    depth = 0
    start: int | None = None
    escaped = False
    identities: list[str] = []
    keys: set[str] = set()
    decoder = json.JSONDecoder()
    for index, character in enumerate(line):
        if start is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                if depth == 1:
                    value_start = index + 1
                    while value_start < len(line) and line[value_start].isspace():
                        value_start += 1
                    if value_start < len(line) and line[value_start] == ":":
                        key = decoder.decode(line[start : index + 1])
                        if key in keys:
                            return ""
                        keys.add(key)
                        if key == field_name:
                            value_start += 1
                            while value_start < len(line) and line[value_start].isspace():
                                value_start += 1
                            value, _ = decoder.raw_decode(line, value_start)
                            if not isinstance(value, str):
                                return ""
                            identities.append(value)
                start = None
        elif character == '"':
            start = index
        elif character in "{[":
            depth += 1
        elif character in "}]":
            depth -= 1
    return identities[0].rsplit("/", 1)[-1] if len(identities) == 1 else ""


def _file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _checkout_root(root: Path) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return Path(result.stdout.strip()).parent if result.returncode == 0 else root


def replay_roots(root: Path, extra: Sequence[Path] = ()) -> tuple[Path, ...]:
    """Explicit local evidence roots shared by feeder, inspector and asset store."""
    checkout = _checkout_root(root)
    roots = [
        root,
        checkout / "derived",
        checkout.parent / "eval-lab-results",
        checkout.parent / "eval-lab-data/tb4-20261020",
        *extra,
    ]
    return tuple(dict.fromkeys(path.resolve() for path in roots if path.is_dir()))


@dataclass(frozen=True)
class _Recording:
    item: CorpusItem
    path: Path
    witness: bool = False
    sha256: str | None = None


@dataclass
class ReplaySummary:
    """A count-only receipt; sealed identifiers and operator credentials never enter it."""

    runs_started: int = 0
    runs_completed: int = 0
    runs_controlled: int = 0
    trace_steps: int = 0
    skipped_sealed: int = 0
    skipped_unavailable: int = 0
    skipped_recording_identity: int = 0
    skipped_snapshot_mismatch: int = 0
    snapshots_unverified: int = 0
    snapshot_missing_digests: dict[str, int] = field(default_factory=dict)
    cycles: int = 0
    interrupted: bool = False
    event_types: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ReplayFeeder:
    """Single executor for recorded traces and confirmed TB4 witness differentials.

    ``limit`` bounds recordings per cycle, not steps within a real trajectory.
    ``speed`` is replay events/second; original step timestamps are retained.
    Construction admits source identities before opening trajectory/witness bytes.
    """

    def __init__(
        self,
        queue: ContinuousWorkQueue,
        registry: CorpusRegistry,
        *,
        source: str = "both",
        speed: float = 20,
        limit: int | None = None,
        allowed_roots: Sequence[Path] | None = None,
        tb4_port_root: Path | None = None,
        stop: threading.Event | None = None,
    ) -> None:
        if source not in _DATASETS:
            raise ValueError("source must be tb4-dev, mimo or both")
        if not math.isfinite(speed) or speed <= 0:
            raise ValueError("speed must be finite and positive")
        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")
        self.queue, self.registry = queue, registry
        self.speed, self.limit = speed, limit
        self.roots = (
            tuple(allowed_roots) if allowed_roots is not None else replay_roots(registry.repo_root)
        )
        self.stop = stop if stop is not None else threading.Event()
        sealed = registry.items(split="test")
        self.summary = ReplaySummary(
            skipped_sealed=sum(item.dataset_id in _DATASETS[source] for item in sealed)
        )
        self._sealed_digests = {
            item.task_digest.removeprefix("sha256:") for item in sealed if item.task_digest
        }
        self._task_index: dict[tuple[str, str], list[CorpusItem]] = {}
        for dataset in _DATASETS[source]:
            for item in registry.items(dataset_id=dataset):
                if item.task_id:
                    key = dataset, item.task_id.rsplit("/", 1)[-1]
                    self._task_index.setdefault(key, []).append(item)
        self._run_states: dict[str, str] = {}
        self._run_items: dict[str, CorpusItem] = {}
        self._cursor = 0
        self._session = uuid4().hex[:12]
        self._executor = f"public-replay:{self._session}"
        self._monitor = MisalignmentLiveWorker(emit=self._record)
        self._port_root = self._resolve_port_root(tb4_port_root)
        groups = [self._recordings(dataset) for dataset in _DATASETS[source]]
        self._sources = []
        # Round-robin keeps --source both useful even with a small --limit.
        while groups and (limit is None or len(self._sources) < limit):
            remaining = []
            for group in groups:
                recording = next(group, None)
                if recording is not None:
                    self._sources.append(recording)
                    remaining.append(group)
                    if limit is not None and len(self._sources) >= limit:
                        break
            groups = remaining

    def _resolve_port_root(self, configured: Path | None) -> Path | None:
        candidates = (
            [configured]
            if configured is not None
            else [
                self.registry.repo_root / "derived/public-replay/tb4/karotte-port",
                *[root / "karotte-port" for root in self.roots],
            ]
        )
        for candidate in candidates:
            path = resolve_within(self.roots, candidate)
            if path.is_dir():
                return path
        return None

    def _admit(self, item: CorpusItem) -> CorpusItem | None:
        try:
            admitted = self.registry.admit(item.dataset_id, item.item_id)
        except SplitLockError:
            return None
        return admitted if admitted.split == "dev" else None

    def _identity_item(self, dataset: str, name: str) -> CorpusItem | None:
        name = name.rsplit("/", 1)[-1]
        matches = self._task_index.get((dataset, name), [])
        if not matches or any(item.split != "dev" for item in matches):
            return None
        canonical = [item for item in matches if item.item_id == item.task_id]
        candidates = canonical or matches
        return self._admit(candidates[0]) if len(candidates) == 1 else None

    def _recording_item(
        self, path: Path, dataset: str, *, identity: str | None = None, sha256: str | None = None
    ) -> CorpusItem | None:
        path = resolve_within(self.roots, path)
        metadata = resolve_within(self.roots, path.parent.parent / "result.json")
        names = [] if identity is None else [identity.rsplit("/", 1)[-1]]
        if metadata.is_file():
            material = metadata.read_text(encoding="utf-8")
            recorded_name = _inventory_task(material, field_name="task_name")
            if not recorded_name or self._identity_item(dataset, recorded_name) is None:
                return None
            names.append(recorded_name)
            result = json.loads(material, object_pairs_hook=_unique_object)
            task_config = (result.get("config") or {}).get("task") or {}
            for value in (result.get("task_name"), result.get("task_id"), task_config.get("name")):
                if isinstance(value, str) and value:
                    names.append(value.rsplit("/", 1)[-1])
            recorded_digest = result.get("task_digest")
            if (
                isinstance(recorded_digest, str)
                and recorded_digest.removeprefix("sha256:") in self._sealed_digests
            ):
                return None
        elif identity is None or sha256 is None:
            return None
        if len(set(names)) != 1:
            return None
        return self._identity_item(dataset, names[0])

    def _recordings(self, dataset: str) -> Iterator[_Recording]:
        items = self.registry.items(dataset_id=dataset, split="dev")
        witnesses = [item for item in items if item.metadata.get("witness", {}).get("confirmed")]
        if witnesses:
            item = self._admit(witnesses[0])
            if item is not None:
                yield _Recording(item, resolve_within(self.roots, item.source_path), witness=True)
        seen: set[Path] = set()
        for relative in ("research", "library"):
            base = resolve_within(self.roots, self.registry.repo_root / relative)
            for candidate in sorted(base.rglob("trajectory.json")):
                try:
                    path = resolve_within(self.roots, candidate)
                    if path in seen:
                        continue
                    item = self._recording_item(path, dataset)
                except (OSError, ValueError):
                    item = None
                if item is None:
                    self.summary.skipped_recording_identity += 1
                    continue
                seen.add(path)
                yield _Recording(item, path)
        if dataset == "mimo-defect-map-v1":
            yield from self._mimo_inventory(seen)
        for item in witnesses[1:]:
            admitted = self._admit(item)
            if admitted is not None:
                yield _Recording(
                    admitted, resolve_within(self.roots, admitted.source_path), witness=True
                )

    def _mimo_inventory(self, seen: set[Path]) -> Iterator[_Recording]:
        source_map = resolve_within(
            self.roots,
            self.registry.repo_root / "research/experiments/mimo-defect-map-v1/sources.json",
        )
        if not source_map.is_file():
            return
        sources = json.loads(
            source_map.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )["sources"]
        for source in sources:
            if source["source_id"] != "training_original_inventory":
                continue
            for root in self.roots:
                if root.name != source["repository"]:
                    continue
                inventory = resolve_within(self.roots, root / source["path"])
                if not inventory.is_file():
                    continue
                with inventory.open(encoding="utf-8") as stream:
                    for line in stream:
                        name = _inventory_task(line)
                        item = self._identity_item("mimo-defect-map-v1", name) if name else None
                        if item is None:
                            continue
                        row = json.loads(line, object_pairs_hook=_unique_object)
                        decoded_name = row.get("task")
                        if (
                            not isinstance(decoded_name, str)
                            or decoded_name.rsplit("/", 1)[-1] != name
                            or row.get("heldout")
                        ):
                            self.summary.skipped_recording_identity += 1
                            continue
                        for alias in row.get("provenance", {}).get("aliases", []):
                            if any(
                                isinstance(alias.get(field), str)
                                and alias[field].removeprefix("sha256:") in self._sealed_digests
                                for field in ("executed_task_digest", "source_digest")
                            ):
                                self.summary.skipped_sealed += 1
                                continue
                            trajectory = alias.get("files", {}).get("trajectory")
                            if not trajectory:
                                continue
                            try:
                                path = resolve_within(self.roots, trajectory["path"])
                                if path in seen or not path.is_file():
                                    continue
                                bound = self._recording_item(
                                    path,
                                    item.dataset_id,
                                    identity=decoded_name,
                                    sha256=trajectory.get("sha256"),
                                )
                            except (OSError, ValueError):
                                bound = None
                            if bound is None or bound.item_id != item.item_id:
                                self.summary.skipped_recording_identity += 1
                                continue
                            seen.add(path)
                            yield _Recording(item, path, sha256=trajectory.get("sha256"))
                            break

    def _record(self, event: dict[str, Any]) -> dict[str, Any]:
        payload = {**event.get("payload", {}), "source_kind": "public_replay"}
        if event["event_type"] == "monitor.gate_decided":
            payload.update(
                label_source="monitor.pre_action_gate",
                decision_scope="counterfactual_recorded_action",
            )
        event = {**event, "payload": payload}
        stored = self.queue.append_event(event)
        self.summary.event_types[stored["event_type"]] = (
            self.summary.event_types.get(stored["event_type"], 0) + 1
        )
        return stored

    def _controls(self) -> None:
        while batch := self.queue.read_events(after_seq=self._cursor, limit=500):
            for event in batch:
                self._cursor = event["seq"]
                run_id = event["run_id"]
                if (
                    event["event_type"] != "operator.control_requested"
                    or run_id not in self._run_states
                ):
                    continue
                requested = self._monitor.dispatch_request(event)
                request_id = requested["payload"]["request_id"]
                if self._run_states[run_id] != "running":
                    self._monitor.expire_control(run_id, request_id, reason="run_not_active")
                    continue
                self._monitor.acknowledge_control(run_id, request_id, executor_id=self._executor)
                applied = self._monitor.apply_control(
                    run_id, request_id, executor_id=self._executor
                )
                self.summary.runs_controlled += 1
                self._run_states[run_id] = applied["payload"]["run_state"]

    def _ready(self, run_id: str, *, delay: bool = True) -> bool:
        deadline = time.monotonic() + (1 / self.speed if delay else 0)
        while True:
            self._controls()
            if self.stop.is_set() or self._run_states[run_id] != "running":
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            self.stop.wait(min(0.05, remaining))

    def _emit(
        self,
        run_id: str,
        item: CorpusItem,
        event_type: str,
        payload: dict[str, Any],
        *,
        prefix_step: int | None = None,
        system: str = "misalignment",
        status: str = "confirmed",
    ) -> dict[str, Any]:
        event = self._record(
            {
                "event_id": str(uuid4()),
                "run_id": run_id,
                "system": system,
                "event_type": event_type,
                "seq": 0,
                "ts": datetime.now(UTC).isoformat(),
                "prefix_step": prefix_step,
                "status": status,
                "payload": {
                    **payload,
                    "dataset_id": item.dataset_id,
                    "item_id": item.item_id,
                    "task_id": item.task_id,
                    "source_kind": "public_replay",
                    "executor_id": self._executor,
                },
            }
        )
        if event_type == "run.started" or event_type.startswith("trace."):
            self._monitor.ingest_event(event)
        return event

    def _tree_digest(self, root: Path, *, port: bool = False) -> str:
        files: dict[str, str] = {}
        for candidate in sorted(root.rglob("*")):
            path = resolve_within((root,), candidate)
            if (
                port
                and "__pycache__" not in candidate.parts
                and not candidate.name.endswith(".pyc")
            ) and path.is_file():
                files[candidate.relative_to(root).as_posix()] = _file_sha256(path)
        if not port:
            return task_directory_digest(root)
        # Exact gt2_port_full.py tree_digest: its staged task includes materialized data.
        material = "".join(f"{name}\0{digest}\n" for name, digest in sorted(files.items()))
        return "sha256:" + hashlib.sha256(material.encode()).hexdigest()

    def _port_receipt(self, witness: dict[str, Any]) -> dict[str, Any] | None:
        replay = witness["port_replay"]
        expected = replay.get("receipt_sha256")
        if not expected:
            return None
        candidates = (
            [Path(replay["receipt"])]
            if replay.get("receipt")
            else [
                root / "2026-10-20/tb4-audit-gt2/port" / f"{witness['witness_id']}.score.json"
                for root in self.roots
                if root.name == "eval-lab-results"
            ]
        )
        for candidate in candidates:
            path = resolve_within(self.roots, candidate)
            if not path.is_file():
                continue
            if _file_sha256(path) != expected.removeprefix("sha256:"):
                raise ValueError("recorded port receipt digest mismatch")
            receipt = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
            if receipt.get("witness_id") != witness["witness_id"] or receipt.get("split") != "dev":
                raise ValueError("recorded port receipt identity mismatch")
            return receipt
        return None

    def _witness_payload(self, item: CorpusItem) -> dict[str, Any] | None:
        witness = item.metadata["witness"]
        original = witness.get("upstream_replay", {})
        repaired = witness.get("port_replay", {})
        if (
            original.get("reward") != 1
            or repaired.get("reward") != 0
            or repaired.get("mode") != "full"
        ):
            return None
        if self._port_root is None:
            return None
        package = item.metadata.get("package") or {}
        if not package.get("root_hint") or not package.get("name"):
            return None
        original_task = resolve_within(
            self.roots, Path(package["root_hint"]).expanduser() / package["name"]
        )
        repaired_task = resolve_within(self.roots, self._port_root / package["name"])
        if original_task.name != package["name"] or repaired_task.name != package["name"]:
            self.summary.skipped_recording_identity += 1
            return None
        if not original_task.is_dir() or not repaired_task.is_dir():
            return None
        bundle = resolve_within(self.roots, item.source_path.parent / witness["solution_dir"])
        files = {}
        for name, expected in witness["files"].items():
            path = resolve_within((bundle,), bundle / name)
            if not path.is_file() or _file_sha256(path) != expected.removeprefix("sha256:"):
                raise ValueError("recorded witness file digest mismatch")
            text, truncated = read_bounded_text(path)
            if truncated:
                raise ValueError("recorded witness exceeds the 64 KiB export preview bound")
            files[name] = text
        receipt = self._port_receipt(witness)
        custody = receipt.get("custody", {}) if receipt else {}
        reference = custody.get("port_scoring_script") or repaired.get("port_scoring_script")
        expected_original = item.task_digest
        expected_repaired = repaired.get("port_task_tree_sha256") or repaired.get(
            "task_tree_sha256"
        )
        expected_scorer = repaired.get("port_scoring_script_sha256")
        port_algorithm = bool(repaired.get("port_task_tree_sha256"))
        checks: dict[str, Any] = {
            "original": {
                "expected": expected_original,
                "actual": self._tree_digest(original_task),
                "algorithm": "evallab.registry.task_directory_digest",
            },
            "repaired": {
                "expected": expected_repaired,
                "actual": self._tree_digest(repaired_task, port=port_algorithm),
                "algorithm": (
                    "tb4.audit.gt2.port-replay/v2:relpath-NUL-sha256-LF"
                    if port_algorithm
                    else "evallab.registry.task_directory_digest"
                ),
            },
            "scorer": {"path": reference, "expected": expected_scorer, "actual": None},
        }
        if reference:
            scorer = resolve_within((repaired_task,), repaired_task / reference)
            checks["scorer"]["actual"] = _file_sha256(scorer)
        missing = [name for name, check in checks.items() if not check["expected"]]
        if not reference:
            missing.append("scorer_reference")
        if repaired.get("receipt_sha256") and receipt is None:
            missing.append("port_receipt")
        mismatches = [
            name
            for name, check in checks.items()
            if check["expected"]
            and str(check["expected"]).removeprefix("sha256:")
            != str(check["actual"]).removeprefix("sha256:")
        ]
        if custody and (
            str(custody.get("port_task_tree_sha256")).removeprefix("sha256:")
            != str(expected_repaired).removeprefix("sha256:")
            or str(custody.get("port_scoring_script_sha256")).removeprefix("sha256:")
            != str(expected_scorer).removeprefix("sha256:")
        ):
            raise ValueError("recorded port receipt differs from witness custody")
        verified = not missing and not mismatches
        if not verified:
            self.summary.snapshots_unverified += 1
            self.summary.skipped_snapshot_mismatch += bool(mismatches)
            for name in missing:
                self.summary.snapshot_missing_digests[name] = (
                    self.summary.snapshot_missing_digests.get(name, 0) + 1
                )
        return {
            "kind": "exploit_witness",
            "summary": item.metadata.get("title", witness["witness_id"]),
            "finding": {
                "mechanism": item.metadata.get("title"),
                "taxonomy_class": item.metadata.get("taxonomy_class"),
            },
            "original_task": str(original_task),
            "repaired_task": str(repaired_task),
            "solution_bundle": str(bundle),
            "exploit_files": files,
            "candidate_digest": "sha256:"
            + hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
            "snapshot_status": "verified" if verified else "snapshot_unverified",
            "snapshot_checks": checks,
            "snapshot_missing": missing,
            "snapshot_mismatches": mismatches,
            "recorded_rewards": {"original": original["reward"], "repaired": repaired["reward"]},
            **(
                {
                    "original_reward": original["reward"],
                    "repaired_reward": repaired["reward"],
                    "passing_runs": [original],
                    "repaired_runs": [repaired],
                }
                if verified
                else {}
            ),
            "recorded_witness_id": witness["witness_id"],
            "qualification": "recorded differential, not a fresh run or nine-control repair qualification",
        }

    def _replay(self, source: _Recording, index: int) -> None:
        item = self._admit(source.item)
        if item is None:
            self.summary.skipped_sealed += 1
            return
        if source.witness:
            payload = self._witness_payload(item)
            if payload is None:
                self.summary.skipped_unavailable += 1
                return
            steps = []
        else:
            path = resolve_within(self.roots, source.path)
            bound = self._recording_item(
                path, item.dataset_id, identity=item.task_id, sha256=source.sha256
            )
            if bound is None or bound.item_id != item.item_id:
                self.summary.skipped_recording_identity += 1
                return
            material = path.read_bytes()
            if source.sha256 and hashlib.sha256(material).hexdigest() != source.sha256.removeprefix(
                "sha256:"
            ):
                raise ValueError("recorded trajectory digest mismatch")
            document = json.loads(material, object_pairs_hook=_unique_object)
            steps = sorted(document["steps"], key=lambda step: int(step["step_id"]))
            payload = {
                "recorded_trajectory": str(path),
                "recorded_schema": document.get("schema_version"),
            }
        run_id = f"replay-{self._session}-{self.summary.cycles}-{index}"
        self._run_states[run_id] = "running"
        self._run_items[run_id] = item
        self.summary.runs_started += 1
        self._emit(
            run_id,
            item,
            "run.started",
            {
                "recorded_source": str(source.path),
                "replay_kind": "witness" if source.witness else "trajectory",
                "policy_gate_scope": "counterfactual_recorded_action",
            },
        )
        for step in steps:
            if not self._ready(run_id):
                break
            if step.get("source") in {"agent", "assistant"} and step.get("tool_calls"):
                # Evaluate today's policy, without executing or rewriting the historical action.
                self._monitor.pre_action_gate(run_id, step)
            self._emit(
                run_id,
                item,
                "trace.step",
                {**payload, "step": step},
                prefix_step=int(step["step_id"]),
            )
            self.summary.trace_steps += 1
        if source.witness and self._ready(run_id):
            if payload["snapshot_status"] == "verified":
                confirmed = self._emit(
                    run_id,
                    item,
                    "seeker.exploit_confirmed",
                    payload,
                    system="reward_seeker",
                )
                if self._ready(run_id):
                    self._emit(
                        run_id,
                        item,
                        "asset.verified",
                        {**payload, "source_event_ids": [confirmed["event_id"]]},
                        system="reward_seeker",
                    )
            else:
                self._emit(
                    run_id,
                    item,
                    "seeker.replay_observation",
                    payload,
                    system="reward_seeker",
                    status="provisional",
                )
        if self._ready(run_id):
            self._emit(
                run_id,
                item,
                "run.completed",
                {"replay_kind": "witness" if source.witness else "trajectory"},
            )
            self._run_states[run_id] = "completed"
            self.summary.runs_completed += 1

    def run(self, *, loop: bool = False, keep_alive: bool = False) -> ReplaySummary:
        """Replay, retaining the demo control executor until shutdown when requested."""
        try:
            if not self._sources:
                raise ValueError("no admitted local recordings available for the selected source")
            while not self.stop.is_set():
                self.summary.cycles += 1
                for index, source in enumerate(self._sources):
                    if self.stop.is_set():
                        break
                    self._replay(source, index)
                self._controls()
                if not loop:
                    break
                self.stop.wait(1 / self.speed)
            while keep_alive and not self.stop.is_set():
                self._controls()
                self.stop.wait(0.05)
        except KeyboardInterrupt:
            self.stop.set()
        finally:
            self._controls()
            for run_id, state in self._run_states.items():
                if state == "running":
                    self._emit(
                        run_id,
                        self._run_items[run_id],
                        "run.stopped",
                        {"reason": "feeder_interrupted"},
                    )
                    self._run_states[run_id] = "stopped"
            self.summary.interrupted = self.stop.is_set()
            self._monitor.close()
        return self.summary


def run_command(args: argparse.Namespace, root: Path) -> int:
    """CLI handler shared by production demo, serve and replay (all $0/local)."""
    database = args.database
    if args.production_command == "demo":
        if database is None:
            database = Path(tempfile.mkdtemp(prefix="evallab-production-demo-")) / "journal.sqlite"
        elif database.exists():
            raise FileExistsError(
                "demo requires a fresh database; use serve/replay for an existing one"
            )
    elif args.production_command == "serve" and not database.is_file():
        raise FileNotFoundError("serve requires an existing SQLite journal")
    database = database.expanduser().resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    registry = CorpusRegistry(root)
    extra = [*args.allowed_root]
    if getattr(args, "tb4_port_root", None) is not None:
        extra.append(args.tb4_port_root)
    roots = replay_roots(root, extra)
    stop = threading.Event()
    with ContinuousWorkQueue(database, registry=registry) as queue:
        feeder = (
            ReplayFeeder(
                queue,
                registry,
                source=args.source,
                speed=args.speed,
                limit=args.limit,
                allowed_roots=roots,
                tb4_port_root=args.tb4_port_root,
                stop=stop,
            )
            if args.production_command != "serve"
            else None
        )
        if args.production_command == "replay":
            assert feeder is not None
            try:
                feeder.run(loop=args.loop)
            finally:
                print(json.dumps({"production_replay": feeder.summary.to_dict()}, sort_keys=True))
            return 0
        # Server-only dependencies stay lazy; importing a feeder starts no server.
        import uvicorn

        from evallab.production.asset_store import VerifiedAssetStore
        from evallab.production.control_plane import create_app
        from evallab.production.inspector import RunInspector

        token = args.token or secrets.token_urlsafe(24)
        export_root = args.export_root
        if export_root is None and args.production_command == "demo":
            export_root = database.parent / "verified-assets"
        app = create_app(
            queue,
            operator_tokens={token: "demo-operator"},
            asset_store=VerifiedAssetStore(
                queue, registry, export_root=export_root, allowed_roots=roots
            ),
            inspector=RunInspector(queue, registry, allowed_roots=roots),
        )
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning")
        )
        errors: list[Exception] = []

        def feed() -> None:
            while not server.started:
                if stop.wait(0.05):
                    return
            try:
                assert feeder is not None
                feeder.run(loop=args.loop, keep_alive=True)
            except Exception as error:
                errors.append(error)
                server.should_exit = True
            finally:
                assert feeder is not None
                print(
                    json.dumps({"production_replay": feeder.summary.to_dict()}, sort_keys=True),
                    flush=True,
                )

        thread = threading.Thread(target=feed, name="production-public-replay") if feeder else None
        print(f"Live control plane: http://127.0.0.1:{args.port}/", flush=True)
        print(f"Operator token: {token}", flush=True)
        print(f"Journal: {database}", flush=True)
        if feeder is None:
            print(
                "Attached journal only; no workers are started. Control requests require an active executor.",
                flush=True,
            )
        else:
            print(
                "Historical public replay; prefix detection and counterfactual policy gates are computed now. Pause is terminal for a replay run.",
                flush=True,
            )
        if thread is not None:
            thread.start()
        try:
            server.run()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            if thread is not None:
                thread.join()
        if errors:
            raise errors[0]
    return 0
