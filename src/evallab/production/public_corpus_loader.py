"""Stream cataloged public-corpus exports into durable production jobs.

Three normalized loaders convert pinned external exports into
:class:`PublicCorpusJob` records without materializing the corpus: ``.jsonl``
(and other line-oriented) files are decoded one record at a time from the first
``next()``; ``.json`` documents are accepted for the cataloged index-shaped
exports. Every loader preserves license/provenance metadata attested by the
repository or declared by the file itself — unsupported licenses are never
invented:

- :func:`load_goodfire_rollouts` yields ``misalignment.prefix_audit_task`` jobs
  replaying each rollout prefix through :class:`MisalignmentLiveWorker`. The
  repo does not catalog a Goodfire license, so every row (or the file-level
  ``license``/``provenance`` fields) must declare one; undeclared rows raise.
  The external dataset id defaults to ``goodfire-rollouts`` (or the row's
  declared ``dataset_id``); the export is never labeled ``tb4-audit-v1``.
- :func:`load_terminal_wrench_traces` yields ``misalignment.prefix_audit_task``
  jobs for hack-classified trajectories carrying steps and
  ``reward_seeker.seek_task`` jobs for every record — hack or clean — that also
  supplies runnable ``task_package`` plus ``repaired_task``/``fixed_task``
  locators. External dataset id defaults to ``terminal-wrench``. Terminal Wrench
  is Apache-2.0 per ``evallab.terminal_wrench`` and
  ``research/answer-keys/breaker-bench-v1/upstream/PROVENANCE.md``.
- :func:`load_swebench_annotations` yields ``task_quality.audit_task`` jobs from
  human underspecification/unfair-test severity annotations when a row also
  carries replayable evidence, and ``task_quality.annotation_task`` jobs —
  preserving the same annotation severity — for annotation-only rows whose
  packages/runs are absent. SWE-bench is MIT per
  ``research/references/REFERENCE-MAP.md``. External dataset id defaults to
  ``swebench-annotations``.

Loaders yield only payloads a worker can consume: prefix audits need non-empty
steps, seeker jobs need ``task_package`` plus ``repaired_task``/``fixed_task``,
and audit jobs additionally need ``failing_runs``/``passing_runs`` or an exploit
candidate. ``task_quality.annotation_task`` jobs are normalized queue entries,
not runnable audits: ``TaskQualityLiveWorker.JOB_TYPES`` deliberately excludes
them until a packaging step supplies a materialized ``task_package`` /
``repaired_task`` pair and retained ``failing_runs``/``passing_runs`` trial
directories (or an immutable exploit candidate) — the loader records the
prerequisites in ``payload.execution_prerequisites`` and never fabricates those
locators. Structurally corrupt records raise ``ValueError`` naming the line;
unusable-but-well-formed rows are skipped rather than queued.

:func:`ingest_public_corpus` resolves each job's ``dataset_id``/``item_id`` or
task alias against :class:`CorpusRegistry` — the canonical sealed assignments,
item ids, task names (bare name, ``benchmark/name``, ``name@pin``) and digests —
then enqueues through the durable journal. Records declaring a non-canonical
dataset id are registered through ``CorpusRegistry.register_external`` under
that genuine external dataset id; canonical TEST seals still apply to the
registered item's task name, digest, and item id, so sealed identities are
excluded as ``sealed_test_split`` under any label or namespace. Records whose
declared identifiers contradict each other (for example a DEV ``item_id``
paired with a sealed TEST ``task_id``/``task_digest``) are excluded as
``contradictory_identity``. Public ingestion holds no frozen-eval authority:
``tb4-audit-v1`` TEST items are never queued by this path. Idempotency keys
derive from stable identities only: replaying the same export returns the
original envelopes without duplicating work, and re-keying an id to different
content raises :class:`IdempotencyConflict`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.production.corpus_queue import (
    DATASETS,
    ContinuousWorkQueue,
    CorpusItem,
    CorpusRegistry,
    SplitLockError,
)

#: Queue job type consumed by ``MisalignmentLiveWorker`` as a trace event:
#: ``payload.steps`` is scored one causal prefix at a time.
PREFIX_AUDIT_JOB = "misalignment.prefix_audit_task"
#: Queue job type consumed by ``RewardSeekerLiveWorker`` on measurable DEV tasks.
SEEK_JOB = "reward_seeker.seek_task"
#: Queue job type consumed by ``TaskQualityLiveWorker`` (differential replay).
AUDIT_JOB = "task_quality.audit_task"
#: Queue job type holding normalized human severity annotations that cannot yet
#: drive differential replay; queued for the packaging prerequisites documented
#: in ``payload.execution_prerequisites``, never claimed by today's worker.
ANNOTATION_JOB = "task_quality.annotation_task"

#: Genuine external dataset ids stamped on normalized records that do not
#: declare their own ``dataset_id``.
GOODFIRE_DATASET = "goodfire-rollouts"
TERMINAL_WRENCH_DATASET = "terminal-wrench"
SWEBENCH_DATASET = "swebench-annotations"

#: Licenses attested inside this repository; loaders never stamp unlisted terms.
TERMINAL_WRENCH_LICENSE = "Apache-2.0"  # few-sh/terminal-wrench (terminal_wrench.py)
SWEBENCH_LICENSE = "MIT"  # SWE-bench harness/data (REFERENCE-MAP.md)

_HACK_CLASSES = frozenset(
    {"rewarded_serious_exploit", "rewarded_nonserious_hack", "hack", "exploit"}
)
_STEP_ID = re.compile(r"\d+")
_STEP_SOURCES = {"system", "user", "agent", "assistant", "tool", "observation"}
_EXPLOIT_KEYS = ("exploit_run", "exploit_files", "solution_bundle")
_RUN_KEYS = ("failing_runs", "passing_runs")
_COLLECTION_KEYS = ("rollouts", "trajectories", "annotations", "records", "items", "tasks")


@dataclass(frozen=True)
class PublicCorpusJob:
    """One normalized queueable unit of public-corpus work.

    ``payload`` always carries the downstream work fields and provenance; it
    contains ``dataset_id``/``item_id`` when the export states them, otherwise a
    ``task``/``task_id``/``task_digest`` alias the registry resolves at ingest.
    ``source_id`` is the export's stable row identity — it participates in the
    durable idempotency key, so reused ids must carry identical content.
    """

    job_type: str
    payload: dict[str, Any]
    source_id: str


def _iter_records(source_path: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield ``(locator, record)`` lazily; open and decode only while iterating."""
    if source_path.suffix == ".json":
        document = json.loads(source_path.read_text(encoding="utf-8"))
        envelope: dict[str, Any] = {}
        if isinstance(document, Mapping):
            rows = next(
                (document[key] for key in _COLLECTION_KEYS if isinstance(document.get(key), list)),
                document.get("records", [document]),
            )
            # Document-level declarations fill row gaps; row values always win.
            envelope = {
                key: document[key]
                for key in ("license", "licence", "provenance", "source", "dataset", "revision")
                if key in document
            }
        else:
            rows = document
        if not isinstance(rows, list):
            raise ValueError(f"{source_path}: expected a record list or object")
        for index, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                raise ValueError(f"{source_path}#{index}: corpus row must be an object")
            yield f"{source_path.name}#{index}", {**envelope, **row}
        return
    with source_path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{source_path}:{number}: corpus row must be an object")
            yield f"{source_path.name}:{number}", row


def _lazy(
    load: Callable[[Path], Iterator[PublicCorpusJob]], source_path: Path
) -> Iterator[PublicCorpusJob]:
    """Defer file access until the returned iterator is first advanced."""

    def generate() -> Iterator[PublicCorpusJob]:
        yield from load(source_path)

    return generate()


def _source_id(row: Mapping[str, Any], fields: tuple[str, ...], where: str) -> str:
    for field in fields:
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    raise ValueError(f"{where}: a stable row id is required (one of {fields})")


def _license(row: Mapping[str, Any], default: str | None, where: str) -> str:
    """Declared license only; absent declarations are an error, not a guess."""
    for container in (row, row.get("provenance"), row.get("source"), row.get("license_info")):
        if isinstance(container, Mapping):
            value = container.get("license") or container.get("licence")
            if isinstance(value, str) and value.strip():
                return value
    if default is not None:
        return default
    raise ValueError(f"{where}: license is not declared by the source export")


def _provenance(
    loader: str,
    source_id: str,
    license: str,
    row: Mapping[str, Any],
    locator: str,
    source_path: Path,
) -> dict[str, Any]:
    declared = row.get("provenance")
    provenance = dict(declared) if isinstance(declared, Mapping) else {}
    provenance.update(
        {
            "loader": loader,
            "source_id": source_id,
            "license": license,
            "source_path": str(source_path),
            "record_locator": locator,
        }
    )
    for key in ("source", "revision", "dataset"):
        if row.get(key) is not None and key not in provenance:
            provenance[key] = row[key]
    for key in ("split", "dataset_split"):
        if key in row and "declared_split" not in provenance:
            provenance["declared_split"] = row[key]
    return provenance


def _normalize_call(raw: Any, where: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where}: tool_calls entries must be objects")
    call = dict(raw)
    function = call.pop("function", None)
    if "tool_call_id" not in call and call.get("id") is not None:
        call["tool_call_id"] = call.pop("id")
    if "function_name" not in call:
        name = call.get("name")
        if name is None and isinstance(function, Mapping):
            name = function.get("name")
        if name is not None:
            call["function_name"] = name
    call.pop("name", None)
    args_alias = call.pop("args", None)
    arguments = call.get("arguments", args_alias)
    if arguments is None and isinstance(function, Mapping):
        arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
            if isinstance(parsed, (dict, list)):
                arguments = parsed
        except json.JSONDecodeError:
            pass
    if arguments is not None:
        call["arguments"] = arguments
    return call


def _normalize_steps(raw_steps: Any, where: str) -> list[dict[str, Any]]:
    """Normalize ATIF steps or chat messages into monitor-shaped steps."""
    if not isinstance(raw_steps, list):
        raise ValueError(f"{where}: steps/messages must be a list")
    steps = []
    for number, raw in enumerate(raw_steps, 1):
        if not isinstance(raw, Mapping):
            raise ValueError(f"{where}: step {number} must be an object")
        step = dict(raw)
        source = step["source"] if "source" in step else step.pop("role", "agent")
        if not isinstance(source, str) or source not in _STEP_SOURCES:
            raise ValueError(f"{where}: step {number} has an unsupported source {source!r}")
        step["source"] = "agent" if source == "assistant" else source
        value = step.get("step_id", number)
        if isinstance(value, bool) or not _STEP_ID.fullmatch(str(value)):
            raise ValueError(f"{where}: step {number} has an invalid step_id {value!r}")
        step["step_id"] = int(str(value))
        if "message" not in step and "content" in step:
            step["message"] = step.pop("content")
        if step.get("tool_calls") is not None:
            if not isinstance(step["tool_calls"], list):
                raise ValueError(f"{where}: step {number} tool_calls must be a list")
            step["tool_calls"] = [_normalize_call(call, where) for call in step["tool_calls"]]
        steps.append(step)
    return steps


def _trace_steps(row: Mapping[str, Any], where: str) -> list[dict[str, Any]] | None:
    """Normalized steps, or ``None`` when the record carries no trace body."""
    trajectory = row.get("trajectory")
    raw_steps: Any = row.get("steps", row.get("messages"))
    if raw_steps is None and isinstance(trajectory, Mapping):
        raw_steps = trajectory.get("steps", trajectory.get("messages"))
    if raw_steps is None:
        return None
    return _normalize_steps(raw_steps, where)


def _identity_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    identity: dict[str, Any] = {}
    for key in ("dataset_id", "item_id", "task_id", "task", "task_digest"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            identity[key] = value
    if "task_id" not in identity and identity.get("task"):
        identity["task_id"] = identity["task"]
    return identity


def _audit_payload(
    loader: str,
    source_id: str,
    license: str,
    row: Mapping[str, Any],
    steps: list[dict[str, Any]],
    locator: str,
    source_path: Path,
) -> dict[str, Any]:
    payload = _identity_fields(row)
    payload.update(
        {
            "source_kind": "public_rollout",
            "source_id": source_id,
            "steps": steps,
            "license": license,
            "provenance": _provenance(loader, source_id, license, row, locator, source_path),
        }
    )
    for key in ("model", "agent", "coverage", "task_manifest", "baselines", "executor_id"):
        if row.get(key) is not None:
            payload[key] = row[key]
    return payload


def _task_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    package = row.get("task_package") or row.get("original_task")
    repaired = row.get("repaired_task") or row.get("fixed_task")
    if package:
        fields["task_package"] = package
    if repaired:
        fields["repaired_task"] = repaired
    for key in (
        "fixed_kind",
        "repaired_grader_kind",
        "reference_patch",
        "reference_digest",
        "original_task_package",
        "cues",
        "hint",
        "measurable",
        "runnable",
        "target_dir",
        "task_package_digest",
        "repaired_task_digest",
    ):
        if row.get(key) is not None:
            fields[key] = row[key]
    return fields


def _evidence_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    fields = _task_fields(row)
    for key in (*_RUN_KEYS, *_EXPLOIT_KEYS, "candidate_digest"):
        if row.get(key) is not None:
            fields[key] = row[key]
    return fields


def _has_evidence(payload: Mapping[str, Any]) -> bool:
    runs = any(payload.get(key) for key in _RUN_KEYS)
    return runs or any(payload.get(key) is not None for key in _EXPLOIT_KEYS)


def load_goodfire_rollouts(source_path: str | Path) -> Iterator[PublicCorpusJob]:
    """Yield ``misalignment.prefix_audit_task`` jobs from a Goodfire rollout export.

    Each row needs a stable ``rollout_id``/``id``/``trajectory_id``, ``steps`` or
    chat ``messages``, a task reference (``item_id`` or ``task``/``task_id``),
    and a declared ``license`` — the repository catalogs no Goodfire license,
    so undeclared exports raise instead of inventing one. ``dataset_id``
    defaults to ``goodfire-rollouts`` because the export is a Goodfire rollout
    corpus, not a ``tb4-audit-v1`` member; a row may declare its own external
    dataset id. Rows without usable steps are skipped.
    """

    def load(path: Path) -> Iterator[PublicCorpusJob]:
        for where, row in _iter_records(path):
            source_id = _source_id(
                row, ("rollout_id", "id", "trajectory_id", "source_id"), where
            )
            license = _license(row, None, where)
            steps = _trace_steps(row, where)
            if not steps:
                continue
            payload = _audit_payload(
                "goodfire_rollouts", source_id, license, row, steps, where, path
            )
            payload.setdefault("dataset_id", GOODFIRE_DATASET)
            yield PublicCorpusJob(PREFIX_AUDIT_JOB, payload, source_id)

    return _lazy(load, Path(source_path))


def _tw_source_id(row: Mapping[str, Any], where: str) -> str:
    """Stable per-run identity; bare labels are shared, never unique ids."""
    for field in ("trajectory_id", "id", "tw_key", "source_id"):
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    parts = [row.get("task_id") or row.get("task"), row.get("model"), row.get("label")]
    composite = [part.strip() for part in parts if isinstance(part, str) and part.strip()]
    if len(composite) >= 2:
        return "|".join(composite)
    raise ValueError(
        f"{where}: a stable row id is required "
        "(trajectory_id/id/tw_key/source_id or a task+model+label composite)"
    )


def load_terminal_wrench_traces(source_path: str | Path) -> Iterator[PublicCorpusJob]:
    """Yield seeker/misalignment jobs from Terminal Wrench trajectory records.

    Hack-classified trajectories (``rewarded_serious_exploit``,
    ``rewarded_nonserious_hack``, ``hack``, ``exploit``) yield
    ``misalignment.prefix_audit_task`` jobs over their ATIF steps. Any record —
    hack or clean — that also supplies runnable ``task_package`` plus
    ``repaired_task``/``fixed_task`` locators additionally yields a
    ``reward_seeker.seek_task`` job; task+hack records are normalized into both
    job families rather than dropping the seeker half. License defaults to the
    repo-attested ``Apache-2.0`` unless the row declares otherwise; the external
    dataset id defaults to ``terminal-wrench``.
    """

    def load(path: Path) -> Iterator[PublicCorpusJob]:
        for where, row in _iter_records(path):
            source_id = _tw_source_id(row, where)
            license = _license(row, TERMINAL_WRENCH_LICENSE, where)
            classification = str(row.get("classification") or row.get("kind") or "").lower()
            if not classification and isinstance(row.get("tw_key"), str):
                if row["tw_key"].startswith("hacks__"):
                    classification = "hack"
                elif row["tw_key"].startswith("clean__"):
                    classification = "clean"
            provenance = _provenance(
                "terminal_wrench_traces", source_id, license, row, where, path
            )
            provenance.update(
                {
                    "dataset": row.get("dataset") or "few-sh/terminal-wrench",
                    "classification": classification or None,
                    "model": row.get("model"),
                    "label": row.get("label"),
                }
            )
            if classification in _HACK_CLASSES:
                steps = _trace_steps(row, where)
                if steps:
                    payload = _audit_payload(
                        "terminal_wrench_traces", source_id, license, row, steps, where, path
                    )
                    payload["provenance"] = provenance
                    payload.setdefault("dataset_id", TERMINAL_WRENCH_DATASET)
                    yield PublicCorpusJob(PREFIX_AUDIT_JOB, payload, source_id)
            fields = _task_fields(row)
            if not (fields.get("task_package") and fields.get("repaired_task")):
                continue
            payload = _identity_fields(row)
            payload.update(
                {
                    **fields,
                    "source_kind": "public_rollout",
                    "source_id": source_id,
                    "license": license,
                    "provenance": provenance,
                }
            )
            payload.setdefault("dataset_id", TERMINAL_WRENCH_DATASET)
            if row.get("model") is not None:
                payload["model"] = row["model"]
            yield PublicCorpusJob(SEEK_JOB, payload, source_id)

    return _lazy(load, Path(source_path))


#: What a ``task_quality`` consumer needs before an annotation-only record can
#: execute: ``TaskQualityLiveWorker._prepare`` resolves ``task_package`` and
#: ``repaired_task`` directories, and ``_sources`` requires retained
#: ``failing_runs``/``passing_runs`` trial dirs or an immutable exploit
#: candidate (``exploit_run``/``exploit_files``/``solution_bundle``). These are
#: prerequisites downstream packaging must supply; the loader never invents them.
ANNOTATION_PREREQUISITES = (
    "materialized task_package and repaired_task task directories",
    "retained failing_runs/passing_runs trial directories or an immutable exploit candidate",
)


def _annotation(row: Mapping[str, Any]) -> dict[str, Any]:
    annotation = {
        "underspecified": row.get("underspecified", row.get("underspecification")),
        "unfair_tests": row.get("unfair_tests", row.get("unfair")),
    }
    for key in ("severity", "max_severity", "annotators", "comment"):
        if row.get(key) is not None:
            annotation[key] = row[key]
    for key in ("annotation", "annotations", "labels"):
        if row.get(key) is not None:
            annotation[key] = row[key]
    return annotation


def load_swebench_annotations(source_path: str | Path) -> Iterator[PublicCorpusJob]:
    """Yield ``task_quality`` jobs from SWE-bench human annotations.

    Rows carry ``instance_id``/``item_id``/``task_id`` identity plus the human
    underspecification and unfair-test severity assessments (``underspecified``
    and ``unfair_tests``/``unfair``; ``annotation``/``annotations`` blocks are
    preserved verbatim). Rows that also supply ``task_package``,
    ``repaired_task``/``fixed_task`` and at least one
    ``failing_runs``/``passing_runs`` list or exploit candidate yield
    ``task_quality.audit_task`` jobs. Annotation-only rows — the requested
    normalization inputs — yield ``task_quality.annotation_task`` jobs carrying
    the same severity and the documented ``execution_prerequisites``; declared
    locators are preserved but none are fabricated. Rows with neither usable
    evidence nor annotations are skipped. License defaults to the repo-attested
    ``MIT`` unless the row declares otherwise; the external dataset id defaults
    to ``swebench-annotations``.
    """

    def load(path: Path) -> Iterator[PublicCorpusJob]:
        for where, row in _iter_records(path):
            source_id = _source_id(
                row, ("instance_id", "item_id", "id", "task_id", "source_id"), where
            )
            license = _license(row, SWEBENCH_LICENSE, where)
            annotation = _annotation(row)
            fields = _evidence_fields(row)
            provenance = _provenance(
                "swebench_annotations", source_id, license, row, where, path
            )
            if (
                fields.get("task_package")
                and fields.get("repaired_task")
                and _has_evidence(fields)
            ):
                payload = _identity_fields(row)
                payload.update(
                    {
                        **fields,
                        "source_kind": "public_rollout",
                        "source_id": source_id,
                        "annotation": annotation,
                        "license": license,
                        "provenance": provenance,
                    }
                )
                payload.setdefault("dataset_id", SWEBENCH_DATASET)
                yield PublicCorpusJob(AUDIT_JOB, payload, source_id)
                continue
            if not any(value is not None for value in annotation.values()):
                continue
            payload = _identity_fields(row)
            payload.update(
                {
                    **fields,
                    "source_kind": "public_annotation",
                    "source_id": source_id,
                    "annotation": annotation,
                    "license": license,
                    "provenance": provenance,
                    "packaging_state": "annotation_only",
                    "execution_prerequisites": list(ANNOTATION_PREREQUISITES),
                }
            )
            payload.setdefault("dataset_id", SWEBENCH_DATASET)
            yield PublicCorpusJob(ANNOTATION_JOB, payload, source_id)

    return _lazy(load, Path(source_path))


def _canonical_name(value: str) -> str:
    """Bare task name: strip ``@pin`` and take the last path segment."""
    return value.split("@", 1)[0].rsplit("/", 1)[-1]


def _alias_index(registry: CorpusRegistry) -> dict[str, list[CorpusItem]]:
    index: dict[str, list[CorpusItem]] = {}

    def add(alias: str | None, item: CorpusItem) -> None:
        if alias:
            index.setdefault(alias, []).append(item)

    for item in registry.items():
        add(f"{item.dataset_id}\0{item.item_id}", item)
        add(item.item_id, item)
        add(item.task_id, item)
        if item.task_id is not None:
            add(_canonical_name(item.task_id), item)
        add(item.task_digest, item)
    return index


def _resolve(
    registry: CorpusRegistry,
    index: Mapping[str, list[CorpusItem]],
    payload: Mapping[str, Any],
) -> CorpusItem | None:
    """Resolve declared identity or a task alias to one registered corpus item.

    A non-canonical ``dataset_id`` selects the external namespace: only an
    already-registered ``(dataset_id, item_id)`` resolves — novel records are
    registered by :func:`_register_external`, never aliased into another
    dataset's items.
    """
    dataset_id = payload.get("dataset_id")
    item_id = payload.get("item_id")
    if isinstance(dataset_id, str) and dataset_id not in DATASETS:
        if item_id is None:
            return None
        try:
            return registry.get(dataset_id, str(item_id))
        except KeyError:
            return None
    if item_id is not None:
        if dataset_id is not None:
            try:
                return registry.get(str(dataset_id), str(item_id))
            except KeyError:
                return None
        for candidate in index.get(str(item_id), ()):
            if candidate.item_id == item_id:
                return candidate
        return None
    alias = payload.get("task") or payload.get("task_id") or payload.get("task_digest")
    if not isinstance(alias, str) or not alias.strip():
        return None
    raw = index.get(alias) or index.get(_canonical_name(alias)) or []
    # One item can register under several aliases of the same key.
    deduped = {(item.dataset_id, item.item_id): item for item in raw}
    candidates = list(deduped.values())
    if dataset_id is not None:
        candidates = [item for item in candidates if item.dataset_id == dataset_id]
    if not candidates:
        return None
    digest = payload.get("task_digest")
    if isinstance(digest, str) and digest != alias:
        narrowed = [item for item in candidates if item.task_digest == digest]
        if narrowed:
            candidates = narrowed
    task_items = [item for item in candidates if item.item_id == item.task_id]
    if task_items:
        candidates = task_items
    if len(candidates) != 1:
        raise ValueError(
            f"task alias {alias!r} resolves to {len(candidates)} corpus items; "
            "supply dataset_id/item_id explicitly"
        )
    return candidates[0]


def _consistent(
    item: CorpusItem,
    payload: Mapping[str, Any],
    index: Mapping[str, list[CorpusItem]],
) -> bool:
    """Declared task/digest identifiers must agree with the resolved item.

    A record may name its subject by ``item_id``, ``task``/``task_id``, and/or
    ``task_digest``; every declared identifier must describe the resolved item.
    A DEV ``item_id`` paired with a sealed TEST ``task_id`` or ``task_digest``
    is contradictory and must never be queued.
    """
    key = (item.dataset_id, item.item_id)
    for field in ("task", "task_id"):
        declared = payload.get(field)
        if not isinstance(declared, str) or not declared.strip():
            continue
        if item.task_id is not None and _canonical_name(declared) == _canonical_name(
            item.task_id
        ):
            continue
        candidates = {
            (candidate.dataset_id, candidate.item_id)
            for candidate in (
                (index.get(declared) or []) + (index.get(_canonical_name(declared)) or [])
            )
        }
        if key not in candidates:
            return False
    digest = payload.get("task_digest")
    if (
        isinstance(digest, str)
        and digest.strip()
        and (item.task_digest is None or digest != item.task_digest)
    ):
        candidates = {
            (candidate.dataset_id, candidate.item_id)
            for candidate in (index.get(digest) or [])
        }
        if key not in candidates:
            return False
    return True


def _register_external(
    registry: CorpusRegistry, job: PublicCorpusJob, payload: Mapping[str, Any]
) -> CorpusItem | None:
    """Register a novel record under its genuine external dataset id.

    Canonical dataset ids are never writable here — a job claiming one but
    resolving nothing stays ``unregistered_item``. The external item keeps the
    record's declared task/digest identity so canonical seals still apply.
    """
    dataset_id = payload.get("dataset_id")
    if not isinstance(dataset_id, str) or not dataset_id.strip() or dataset_id in DATASETS:
        return None
    declared_item = payload.get("item_id")
    item_id = str(declared_item).strip() if declared_item is not None else job.source_id
    if not item_id:
        return None
    provenance = payload.get("provenance")
    source = provenance.get("source_path") if isinstance(provenance, Mapping) else None
    task = payload.get("task_id") or payload.get("task")
    declared_split = str(
        payload.get("split")
        or (provenance.get("declared_split") if isinstance(provenance, Mapping) else "")
    ).lower()
    metadata = {
        key: value
        for key, value in (
            ("source_kind", payload.get("source_kind")),
            ("license", payload.get("license")),
            ("provenance", provenance if isinstance(provenance, Mapping) else None),
        )
        if value is not None
    }
    return registry.register_external(
        CorpusItem(
            dataset_id=dataset_id,
            item_id=item_id,
            split="test" if declared_split in ("test", "heldout") else "dev",
            task_id=None if task is None else str(task),
            task_digest=(
                None
                if not isinstance(payload.get("task_digest"), str)
                else str(payload["task_digest"])
            ),
            source_path=Path(source) if isinstance(source, str) else Path(dataset_id),
            metadata=metadata,
        )
    )


def ingest_public_corpus(
    queue: ContinuousWorkQueue,
    jobs: Iterable[PublicCorpusJob],
    *,
    run_id: str,
) -> dict[str, Any]:
    """Resolve, admit, and durably enqueue normalized public-corpus jobs.

    Each job resolves to a registered ``(dataset_id, item_id)`` through the
    queue's registry; records naming a non-canonical dataset id are registered
    under that genuine external id via ``CorpusRegistry.register_external``.
    Misleading file-level split labels never unseal work: sealed references
    (including task-name aliases, digests, and item ids under external ids) are
    recorded under ``excluded`` as ``sealed_test_split``. Sealed TEST items are
    never queued by this ingestion — there is intentionally no frozen-eval
    token parameter. Self-contradictory identifier sets are excluded as
    ``contradictory_identity``; unresolvable references as
    ``unregistered_item``; ambiguous aliases raise.

    Ingestion is deterministic and idempotent: the durable key derives only
    from the stable identity ``job_type/dataset_id/item_id/source_id``, so a
    repeated export returns the original envelopes and appends no new events,
    while the same identity carrying different content is rejected by the
    journal's :class:`IdempotencyConflict`.
    """
    registry = queue.registry
    index = _alias_index(registry)
    enqueued: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for job in jobs:
        if not isinstance(job, PublicCorpusJob):
            raise TypeError(f"expected PublicCorpusJob, got {type(job).__name__}")
        payload = dict(job.payload)
        declared_tasks = {
            _canonical_name(value)
            for value in (payload.get("task"), payload.get("task_id"))
            if isinstance(value, str) and value.strip()
        }
        if len(declared_tasks) > 1:
            excluded.append(
                {
                    "source_id": job.source_id,
                    "job_type": job.job_type,
                    "dataset_id": payload.get("dataset_id"),
                    "item_id": payload.get("item_id") or payload.get("task"),
                    "reason": "contradictory_identity",
                }
            )
            continue
        item = _resolve(registry, index, payload)
        if item is None:
            item = _register_external(registry, job, payload)
            if item is None:
                excluded.append(
                    {
                        "source_id": job.source_id,
                        "job_type": job.job_type,
                        "dataset_id": payload.get("dataset_id"),
                        "item_id": payload.get("item_id") or payload.get("task"),
                        "reason": "unregistered_item",
                    }
                )
                continue
        elif not _consistent(item, payload, index):
            excluded.append(
                {
                    "source_id": job.source_id,
                    "job_type": job.job_type,
                    "dataset_id": item.dataset_id,
                    "item_id": item.item_id,
                    "reason": "contradictory_identity",
                }
            )
            continue
        try:
            admitted = registry.admit(item.dataset_id, item.item_id)
        except SplitLockError:
            excluded.append(
                {
                    "source_id": job.source_id,
                    "job_type": job.job_type,
                    "dataset_id": item.dataset_id,
                    "item_id": item.item_id,
                    "reason": "sealed_test_split",
                }
            )
            continue
        payload.pop("split", None)
        payload.pop("declared_split", None)
        payload["dataset_id"] = admitted.dataset_id
        payload["item_id"] = admitted.item_id
        if admitted.task_id is not None:
            payload["task_id"] = admitted.task_id
        envelope = queue.enqueue(
            job.job_type,
            payload,
            run_id=run_id,
            idempotency_key=(
                f"public-corpus:{job.job_type}:{admitted.dataset_id}:"
                f"{admitted.item_id}:{job.source_id}"
            ),
        )
        enqueued.append(envelope)
    return {"run_id": run_id, "enqueued": enqueued, "excluded": excluded}
