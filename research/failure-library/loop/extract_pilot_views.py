"""Recompute the frozen pilot views offline; no model, tools, grader or shell.

This evidence helper is not yet the public pilot's admission gate. The integration
owner must bind reconstruction before a subsequent model request.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from evallab.failure_library.loop.readonly import source_view
from evallab.failure_library.loop.selection import digest
from evallab.registry import task_directory_digest


def reconstruct(subject: dict) -> list[str]:
    for source in subject["sources"]:
        with Path(source["path"]).open("rb") as handle:
            assert hashlib.file_digest(handle, "sha256").hexdigest() == source["sha256"]
    if subject["kind"] == "task":
        package = Path(subject["package_path"]).resolve()
        expected = subject["record"]["task"]["task_version_digest"]
        assert expected and task_directory_digest(package) == expected
        assert all(
            Path(source["path"]).resolve().is_relative_to(package) for source in subject["sources"]
        )
        documents = [Path(source["path"]).read_text() for source in subject["sources"]]
    else:
        import pyarrow.parquet as pq

        assert subject["native_ref"] == subject["record"]["transcript_ref"]
        ref = urlparse(subject["native_ref"])
        path, fields = Path(unquote(ref.path)), parse_qs(ref.fragment)
        assert str(path) == subject["sources"][0]["path"]
        offset, field = int(fields["row"][0]), fields["field"][0]
        parquet = pq.ParquetFile(path)
        if not 0 <= offset < parquet.metadata.num_rows:
            raise ValueError("native zero-based row is outside the pinned Parquet")
        messages = None
        for group in range(parquet.num_row_groups):
            count = parquet.metadata.row_group(group).num_rows
            if offset < count:
                messages = parquet.read_row_group(group, columns=[field])[field][offset].as_py()
                break
            offset -= count
        if isinstance(messages, str):
            messages = json.loads(messages)
        documents = []
        for span in subject["spans"]:
            message = messages[span["message"]]
            content = message.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, sort_keys=True)
            assert len(content) == span["original_chars"]
            assert 0 <= span["start"] <= span["end"] <= len(content)
            text = content[span["start"] : span["end"]]
            text = text.replace(subject["record"]["task"]["task_id"], "[subject]")
            documents.append(message.get("role", "unknown").upper() + "\n" + text)
    assert digest(documents) == subject["documents_digest"]
    assert source_view(documents)["sha256"] == subject["view_sha256"]
    return documents


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    frozen = json.loads(args.manifest.read_text())
    assert frozen["digest"] == digest(
        {key: value for key, value in frozen.items() if key != "digest"}
    )
    print(
        json.dumps(
            [
                {"id": subject["id"], "documents_digest": digest(reconstruct(subject))}
                for subject in frozen["subjects"]
            ],
            sort_keys=True,
        )
    )
