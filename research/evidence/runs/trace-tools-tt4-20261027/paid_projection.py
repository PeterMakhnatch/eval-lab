"""Local-only, exact-message admission of frozen native TT-4 inputs.

No optional SDK is imported until preparation/decoding is requested. The caller
supplies the historical directory and a REQUIRED canonical ``secret_safe``
checker, which receives detached, full JSON-mode ChatMessage dumps. This module
never changes frozen messages, truncates history, or admits an empty history.

Receipts are private and content-free. File manifests map SHA-256(relative path)
to SHA-256(file bytes), so neither source filenames nor source run IDs escape.
An existing output is reusable only with its original receipt, exact artifacts,
and freshly revalidated inputs. Interrupted/unbound outputs are retained and
refused, never repaired or rebound in place.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from typing import Any

RECEIPT_NAME = "paid-projection-receipt.json"
RECEIPT_SCHEMA = "evallab.tt4_paid_projection/v1"


class ProjectionError(ValueError):
    """A content-free preparation/cache failure; the message is a fixed category."""


class _MissingHistory(ValueError):
    pass


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_messages(messages: Sequence[Any]) -> bytes:
    """Retain every typed field/default/null; normalize dictionary order only."""
    if not messages:
        raise _MissingHistory
    return _json_bytes(
        [
            message.model_dump(
                mode="json",
                by_alias=False,
                exclude_none=False,
                exclude_unset=False,
                exclude_defaults=False,
            )
            for message in messages
        ]
    )


@lru_cache(maxsize=1)
def _message_adapter() -> Any:
    from inspect_ai.model import ChatMessage
    from pydantic import TypeAdapter

    return TypeAdapter(list[ChatMessage])


def _decode_native_messages(raw: Any) -> list[Any]:
    """Decode Scout's native JSON column without inventing missing message IDs."""
    if raw is None or raw in ("", b""):
        raise _MissingHistory
    if not isinstance(raw, (str, bytes)):
        raise ValueError("invalid_native_messages")
    decoded = json.loads(raw)
    if decoded is None or decoded == []:
        raise _MissingHistory
    if not isinstance(decoded, list) or any(
        not isinstance(message, dict)
        or message.get("role") not in {"system", "user", "assistant", "tool"}
        or "content" not in message
        for message in decoded
    ):
        raise ValueError("invalid_native_messages")
    # Inspect's deserializing context preserves absent/null IDs as None rather
    # than generating new random IDs during decoding. They cannot match the
    # explicit step/message IDs produced by to_transcript.
    return _message_adapter().validate_python(decoded, context={"deserializing": True})


def _native_adapter() -> Any:
    from evallab.readers import scout_clouddb

    scout_clouddb._require_scout()
    return scout_clouddb


def _native_columns() -> set[str]:
    from inspect_scout import transcripts_db_schema

    return set(transcripts_db_schema().names)


def _files(directory: Path) -> list[Path]:
    try:
        if not directory.exists():
            return []
        if not directory.is_dir():
            raise ProjectionError("invalid_projection_directory")
        files = []
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise ProjectionError("unsafe_projection_path")
            if path.is_file():
                files.append(path)
            elif not path.is_dir():
                raise ProjectionError("unsafe_projection_path")
        return files
    except OSError:
        raise ProjectionError("projection_files_unavailable") from None


def _manifest(directory: Path, *, parquet_only: bool = False) -> dict[str, str]:
    manifest = {}
    for path in _files(directory):
        relative = path.relative_to(directory).as_posix()
        if relative == RECEIPT_NAME or (parquet_only and path.suffix != ".parquet"):
            continue
        try:
            manifest[_sha256(relative.encode("utf-8"))] = _sha256(path.read_bytes())
        except OSError:
            raise ProjectionError("projection_files_unavailable") from None
    return manifest


def _parquet_paths(directory: Path) -> list[Path]:
    return [path for path in _files(directory) if path.suffix == ".parquet"]


def _parquet_rows(paths: Sequence[Path], *, full: bool = False) -> Iterable[dict[str, Any]]:
    """Read raw rows before any SDK index can collapse duplicate transcript IDs."""
    import pyarrow.parquet as pq

    try:
        for path in paths:
            parquet = pq.ParquetFile(path)
            names = parquet.schema_arrow.names
            if len(names) != len(set(names)) or not {"transcript_id", "messages"} <= set(names):
                raise ProjectionError("invalid_native_parquet")
            for batch in parquet.iter_batches(
                batch_size=32,
                columns=None if full else ["transcript_id", "messages"],
            ):
                yield from batch.to_pylist()
    except ProjectionError:
        raise
    except Exception:
        raise ProjectionError("native_parquet_unreadable") from None


def _load_frozen(
    paths: Sequence[Path],
    selected: set[str],
) -> dict[str, tuple[bytes | None, str | None]]:
    found: dict[str, tuple[bytes | None, str | None]] = {}
    for row in _parquet_rows(paths):
        opaque = row.get("transcript_id")
        if not isinstance(opaque, str) or opaque not in selected:
            continue
        payload, category = None, None
        try:
            payload = _canonical_messages(_decode_native_messages(row.get("messages")))
        except _MissingHistory:
            category = "frozen_history_missing"
        except Exception:
            category = "frozen_history_invalid"
        if opaque in found:
            previous, previous_category = found[opaque]
            if (
                payload is not None
                and previous == payload
                and previous_category in (None, "frozen_id_duplicate")
            ):
                category = "frozen_id_duplicate"
            else:
                payload, category = None, "frozen_id_conflict"
        found[opaque] = payload, category
    return found


def _project_current(
    db_path: Path,
    keys: Sequence[dict[str, str]],
    adapter: Any,
) -> dict[str, tuple[Any, str | None]]:
    try:
        connection = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
    except Exception:
        raise ProjectionError("current_database_unavailable") from None
    projected = {}
    with closing(connection):
        try:
            connection.execute("BEGIN")
        except Exception:
            raise ProjectionError("current_database_unavailable") from None
        for key in keys:
            transcript, category = None, None
            try:
                messages = adapter.blind_messages(connection, key["run_id"])
                if not messages:
                    raise _MissingHistory
                transcript = adapter.to_transcript(key["opaque_id"], messages)
            except (adapter.ActorHistoryUnavailable, _MissingHistory):
                category = "actor_history_unavailable"
            except Exception:
                category = "current_history_invalid"
            projected[key["opaque_id"]] = transcript, category
    return projected


def _select_projection(
    keys: Sequence[dict[str, str]],
    frozen: Mapping[str, tuple[bytes | None, str | None]],
    current: Mapping[str, tuple[Any, str | None]],
    metadata: dict[str, str],
    secret_safe: Callable[[list[dict[str, Any]]], bool],
) -> tuple[dict[str, Any], list[Any], dict[str, bytes]]:
    eligible, rejected, hashes, transcripts, payloads = [], [], [], [], {}
    for key in keys:
        opaque = key["opaque_id"]
        source, category = frozen.get(opaque, (None, "frozen_id_missing"))
        transcript, current_category = current.get(opaque, (None, "actor_history_unavailable"))
        payload = None
        if transcript is not None and current_category is None:
            try:
                payload = _canonical_messages(transcript.messages)
                if transcript.transcript_id != opaque or transcript.metadata != metadata:
                    current_category = "current_projection_metadata_invalid"
            except _MissingHistory:
                current_category = "actor_history_unavailable"
            except Exception:
                current_category = "current_history_invalid"
        elif current_category is None:
            current_category = "actor_history_unavailable"
        hashes.append(
            {
                "opaque_id": opaque,
                "source_messages_sha256": _sha256(source) if source is not None else None,
                "current_messages_sha256": _sha256(payload) if payload is not None else None,
            }
        )
        category = category or current_category
        if category is None and source != payload:
            category = "message_drift"
        if category is None:
            try:
                # A detached JSON tree prevents a checker from changing the
                # native objects eventually written by the existing adapter.
                if secret_safe(json.loads(payload)) is not True:
                    category = "secret_unsafe"
            except Exception:
                category = "secret_check_failed"
        if category is not None:
            rejected.append({"opaque_id": opaque, "category": category})
        else:
            eligible.append(opaque)
            transcripts.append(transcript)
            payloads[opaque] = payload
    return (
        {
            "eligible_opaque_ids": eligible,
            "rejected": rejected,
            "message_hashes": hashes,
        },
        transcripts,
        payloads,
    )


def _validate_output(
    paths: Sequence[Path], expected: Mapping[str, bytes], metadata: dict[str, str]
) -> None:
    base = _native_columns()
    content = {"transcript_id", "messages", "events", "timelines", "events_data"}
    seen = set()
    try:
        for row in _parquet_rows(paths, full=True):
            opaque = row.get("transcript_id")
            if not isinstance(opaque, str) or opaque not in expected or opaque in seen:
                raise ProjectionError("projection_output_mismatch")
            if set(row) - base != set(metadata) or any(
                row.get(k) != v for k, v in metadata.items()
            ):
                raise ProjectionError("projection_output_metadata_mismatch")
            if any(row.get(name) is not None for name in base - content):
                raise ProjectionError("projection_output_metadata_mismatch")
            if json.loads(row.get("events") or "[]") != []:
                raise ProjectionError("projection_output_metadata_mismatch")
            if json.loads(row.get("timelines") or "[]") != []:
                raise ProjectionError("projection_output_metadata_mismatch")
            if json.loads(row.get("events_data") or "null") not in (
                None,
                {"messages": [], "calls": []},
            ):
                raise ProjectionError("projection_output_metadata_mismatch")
            if (
                _canonical_messages(_decode_native_messages(row.get("messages")))
                != expected[opaque]
            ):
                raise ProjectionError("projection_output_mismatch")
            seen.add(opaque)
        if seen != set(expected):
            raise ProjectionError("projection_output_mismatch")
    except ProjectionError:
        raise
    except Exception:
        raise ProjectionError("projection_output_invalid") from None


def _keys(keys: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    normalized, opaque_ids, run_ids = [], set(), set()
    for key in keys:
        if (
            not isinstance(key, Mapping)
            or set(key) != {"opaque_id", "run_id"}
            or any(not isinstance(key[name], str) or not key[name] for name in key)
        ):
            raise ProjectionError("invalid_projection_keys")
        if key["opaque_id"] in opaque_ids or key["run_id"] in run_ids:
            raise ProjectionError("duplicate_projection_keys")
        opaque_ids.add(key["opaque_id"])
        run_ids.add(key["run_id"])
        normalized.append({"opaque_id": key["opaque_id"], "run_id": key["run_id"]})
    return normalized


def prepare_projection(
    db_path: str | Path,
    keys: Sequence[Mapping[str, str]],
    frozen_dir: str | Path,
    output_dir: str | Path,
    *,
    secret_safe: Callable[[list[dict[str, Any]]], bool],
) -> dict[str, Any]:
    """Return a private receipt, admitting only frozen/current typed equality.

    ``eligible_opaque_ids``, ``rejected`` and ``message_hashes`` retain input key
    order. Missing/invalid/conflicting frozen histories, unavailable actor
    history, message drift and non-True/failed canonical secret checks abstain
    individually. Global cache/Parquet failures raise content-free
    ``ProjectionError``. Existing outputs are immutable and never extended.

    ``output_dir`` must be a new dedicated directory (parents may exist), or an
    exact output previously produced here. It must not overlap ``frozen_dir``.
    Only the existing native writer and projection-marker guard create Scout
    artifacts. No model, API, credential or remote operation is performed.
    """
    if not callable(secret_safe):
        raise ProjectionError("secret_checker_required")
    ordered = _keys(keys)
    frozen = Path(frozen_dir).expanduser().resolve()
    output_path = Path(output_dir).expanduser()
    output = output_path.resolve()
    if output == frozen or output in frozen.parents or frozen in output.parents:
        raise ProjectionError("projection_directories_overlap")
    if output_path.is_symlink():
        raise ProjectionError("unsafe_projection_path")
    try:
        adapter = _native_adapter()
    except Exception:
        raise ProjectionError("native_sdk_unavailable") from None
    metadata = {
        "blind_schema": adapter.BLIND_SCHEMA,
        "projection_version": adapter.ACTOR_PROJECTION_VERSION,
    }
    existing = output.exists()
    if existing:
        try:
            adapter._check_transcripts(output)
        except Exception:
            raise ProjectionError("stale_projection_cache") from None
        receipt_path = output / RECEIPT_NAME
        if not receipt_path.is_file() or receipt_path.is_symlink():
            raise ProjectionError("unbound_projection_cache")
        try:
            cached = json.loads(receipt_path.read_bytes())
        except Exception:
            raise ProjectionError("invalid_projection_receipt") from None
        if not isinstance(cached, dict):
            raise ProjectionError("invalid_projection_receipt")
    source_files = _manifest(frozen, parquet_only=True)
    old = _load_frozen(_parquet_paths(frozen), {key["opaque_id"] for key in ordered})
    current = _project_current(Path(db_path).expanduser().resolve(), ordered, adapter)
    selection, transcripts, payloads = _select_projection(
        ordered, old, current, metadata, secret_safe
    )
    if source_files != _manifest(frozen, parquet_only=True):
        raise ProjectionError("frozen_input_drift")
    expected = {
        "schema": RECEIPT_SCHEMA,
        "projection_version": adapter.ACTOR_PROJECTION_VERSION,
        "keys_sha256": _sha256(_json_bytes(ordered)),
        "source_parquet_sha256": source_files,
        **selection,
    }
    if existing:
        artifact_fields = {"output_parquet_sha256", "output_file_sha256"}
        if (
            set(cached) != set(expected) | artifact_fields
            or {key: value for key, value in cached.items() if key not in artifact_fields}
            != expected
        ):
            raise ProjectionError("projection_input_drift")
        files = _manifest(output)
        if files != cached["output_file_sha256"] or (
            _manifest(output, parquet_only=True) != cached["output_parquet_sha256"]
        ):
            raise ProjectionError("projection_output_drift")
        _validate_output(_parquet_paths(output), payloads, metadata)
        if files != _manifest(output):
            raise ProjectionError("projection_output_drift")
        return cached
    try:
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
        if transcripts:
            asyncio.run(adapter._insert(output, transcripts))
        else:
            adapter._check_transcripts(output, claim=True)
        _validate_output(_parquet_paths(output), payloads, metadata)
        receipt = {
            **expected,
            "output_parquet_sha256": _manifest(output, parquet_only=True),
            "output_file_sha256": _manifest(output),
        }
        descriptor = os.open(output / RECEIPT_NAME, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_json_bytes(receipt) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        return receipt
    except ProjectionError:
        raise
    except Exception:
        # Preserve partial evidence; without a complete receipt it is forever
        # unbound and the next call refuses it rather than adopting it.
        raise ProjectionError("projection_write_failed") from None
