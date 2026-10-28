"""Recompute blinded diagnostic views from their pinned native source bytes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from evallab.failure_library.records import Finding, RunRecord


def extract_documents(subject: dict[str, Any], record: Finding | RunRecord) -> list[str]:
    """Bind the rendered evidence to a task package or exact native transcript row.

    File SHA checks are the caller's responsibility. This additionally prevents an
    unrelated document from being paired with a genuine source-file checksum.
    Nothing imported from a dataset is executed.
    """
    if isinstance(record, Finding):
        from evallab.registry import task_directory_digest

        if subject.get("view_extractor") != "task-files/v1":
            raise ValueError("task view requires the task-files/v1 extractor")
        package = Path(subject["package_path"]).expanduser().resolve()
        expected = record.task.task_version_digest
        if not expected or task_directory_digest(package) != expected:
            raise ValueError("task package differs from the pinned finding version")
        documents = []
        for source in subject["sources"]:
            path = Path(source["path"]).expanduser().resolve()
            if not path.is_relative_to(package):
                raise ValueError("task view source is outside the pinned package")
            documents.append(path.read_text(encoding="utf-8"))
        return documents

    if subject.get("view_extractor") != "olmo-messages/v1":
        raise ValueError("trace view requires the olmo-messages/v1 extractor")
    if record.collection != "olmo-reward-hacking":
        raise ValueError("native trace extractor supports the public OLMo source only")
    if subject.get("native_ref", record.transcript_ref) != record.transcript_ref:
        raise ValueError("trace extraction locator disagrees with the source record")
    parsed = urlparse(record.transcript_ref)
    fields = parse_qs(parsed.fragment, strict_parsing=True)
    if (
        parsed.scheme != "file"
        or parsed.netloc
        or set(fields) != {"row", "field"}
        or fields["field"] != ["messages"]
        or len(fields["row"]) != 1
    ):
        raise ValueError("trace requires an exact local row/messages source locator")
    index = int(fields["row"][0])
    if index < 0:
        raise ValueError("native row index must be nonnegative")
    path = Path(unquote(parsed.path)).resolve()
    if len(subject["sources"]) != 1 or path != Path(subject["sources"][0]["path"]).resolve():
        raise ValueError("trace source checksum does not bind its native row locator")
    import pyarrow.parquet as pq

    from evallab.failure_library.runs.published import olmo

    parquet = pq.ParquetFile(path)
    if index >= parquet.metadata.num_rows:
        raise ValueError("native transcript row is missing")
    offset, row = index, None
    for group in range(parquet.num_row_groups):
        count = parquet.metadata.row_group(group).num_rows
        if offset < count:
            row = parquet.read_row_group(group).slice(offset, 1).to_pylist()[0]
            break
        offset -= count
    if row is None:
        raise ValueError("native transcript row is missing")
    native = olmo(row, record.transcript_ref).model_copy(
        update={"split": record.split, "finding_ids": record.finding_ids}
    )
    if native != record:
        raise ValueError("trace identity, outcomes or labels disagree with the native row")
    messages = row["messages"]
    if isinstance(messages, str):
        messages = json.loads(messages)
    documents = []
    for span in subject["spans"]:
        message_index, start, end = (span[key] for key in ("message", "start", "end"))
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (message_index, start, end)
        ):
            raise ValueError("native message spans must use integer offsets")
        if not 0 <= message_index < len(messages):
            raise ValueError("native message index is outside the source row")
        message = messages[message_index]
        content = message["content"]
        if not isinstance(content, str):
            content = json.dumps(content, sort_keys=True)
        if span["original_chars"] != len(content) or not 0 <= start < end <= len(content):
            raise ValueError("native message span no longer matches source content")
        text = str(message["role"]).upper() + "\n" + content[start:end]
        # Redact only the exact native identity; no labels or source sidecars enter the view.
        documents.append(text.replace(record.task.task_id, "[subject]"))
    if not documents:
        raise ValueError("trace requires at least one native message span")
    return documents
