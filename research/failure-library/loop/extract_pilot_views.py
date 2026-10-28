"""Reconstruct and bind frozen pilot views offline; no model or source execution.

The public runner uses the same native-source extractor. ``--bind`` regenerates
the manifest's admission metadata only after source/view equality is established.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from evallab.failure_library.loop import source_views
from evallab.failure_library.loop.readonly import source_view
from evallab.failure_library.loop.selection import digest
from evallab.failure_library.records import Finding, RunRecord


def reconstruct(subject: dict) -> list[str]:
    for source in subject["sources"]:
        with Path(source["path"]).open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != source["sha256"]:
                raise ValueError("native source file checksum changed")
    model = Finding if subject["kind"] == "task" else RunRecord
    documents = source_views.extract_documents(subject, model.model_validate(subject["record"]))
    if digest(documents) != subject["documents_digest"]:
        raise ValueError("reconstructed document contents changed")
    if source_view(documents)["sha256"] != subject["view_sha256"]:
        raise ValueError("reconstructed line-cited view changed")
    return documents


def bind_manifest(path: Path, frozen: dict) -> None:
    for subject in frozen["subjects"]:
        subject["view_extractor"] = (
            "task-files/v1" if subject["kind"] == "task" else "olmo-messages/v1"
        )
        reconstruct(subject)
    frozen["reconstruction"] = {
        "script": "research/failure-library/loop/extract_pilot_views.py",
        "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "admission": "native documents and task-package/trace-row identity enforced by public runner",
    }
    frozen["digest"] = digest({key: value for key, value in frozen.items() if key != "digest"})
    path.write_text(json.dumps(frozen, sort_keys=True, indent=2) + "\n")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--bind", action="store_true", help="Regenerate verified source-view admission metadata"
    )
    args = parser.parse_args()
    frozen = json.loads(args.manifest.read_text())
    if frozen["digest"] != digest({key: value for key, value in frozen.items() if key != "digest"}):
        raise ValueError("frozen manifest digest changed")
    if args.bind:
        bind_manifest(args.manifest, frozen)
    print(
        json.dumps(
            [
                {"id": subject["id"], "documents_digest": digest(reconstruct(subject))}
                for subject in frozen["subjects"]
            ],
            sort_keys=True,
        )
    )
