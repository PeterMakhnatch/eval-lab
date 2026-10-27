#!/usr/bin/env python3
"""Verify and profile the local-only bundle, writing counts and digests, never text or raw ids."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import tarfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from adapter import (
    ARCHIVE,
    ARCHIVE_SHA256,
    COMPANION_COMMIT,
    CORPUS,
    HF_REVISION,
    ROOT,
    TRANSCRIPTS,
    TRANSCRIPTS_SHA256,
    UNCOMPRESSED_SHA256,
    hashed_id,
    joined_rows,
    sha256_file,
)

EXPECTED = {
    "observations": 824,
    "hack": 189,
    "strong": 157,
    "weak": 32,
    "unflagged": 635,
    "problems": 41,
    "episodes": 686,
    "shards": 6,
}


def verify_bundle(root: Path) -> dict[str, Any]:
    manifest = json.loads((root / "STAGE-MANIFEST.json").read_text())
    if manifest["revision"] != HF_REVISION or manifest["licence"] != "mit":
        raise ValueError("unexpected HF revision or licence")
    companion = manifest["companion"]
    if companion["commit"] != COMPANION_COMMIT or companion["licence"] != "mit":
        raise ValueError("unexpected companion revision or licence")
    if sha256_file(root / ARCHIVE) != ARCHIVE_SHA256:
        raise ValueError("observation archive SHA-256 mismatch")
    if sha256_file(root / TRANSCRIPTS) != TRANSCRIPTS_SHA256:
        raise ValueError("transcript archive SHA-256 mismatch")
    verified_bytes = 0
    for item in manifest["files"]:
        path = root / item["path"]
        if path.stat().st_size != item["bytes"]:
            raise ValueError("staged file size mismatch")
        digest = sha256_file(path)
        if digest != (item.get("sha256") or item["sha256_computed"]):
            raise ValueError("staged file SHA-256 mismatch")
        if item.get("blob_id") and item.get("verified_against") != "hub_lfs_sha256":
            data = path.read_bytes()
            blob = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
            if blob != item["blob_id"]:
                raise ValueError("staged file Git blob mismatch")
        verified_bytes += item["bytes"]
    if verified_bytes != manifest["total_bytes"]:
        raise ValueError("staging manifest byte total mismatch")
    with gzip.open(root / TRANSCRIPTS, "rb") as stream:
        uncompressed = stream.read()
    if hashlib.sha256(uncompressed).hexdigest() != UNCOMPRESSED_SHA256:
        raise ValueError("uncompressed extraction input SHA-256 mismatch")
    publisher_manifest = json.loads((root / "MANIFEST.json").read_text())
    with tarfile.open(root / ARCHIVE, "r:gz") as archive:
        members = {member.name: member for member in archive.getmembers() if member.isfile()}
        manifest_stream = archive.extractfile("artifacts/OBSERVATION_MANIFEST.json")
        if manifest_stream is None or json.load(manifest_stream) != publisher_manifest:
            raise ValueError("inner/outer publisher manifests differ")
        for item in publisher_manifest["files"]:
            member = members[item["path"]]
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("missing archived file")
            data = stream.read()
            if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise ValueError("publisher archive member digest mismatch")
        cache_manifests = []
        for path in sorted(members):
            if path.endswith("/manifest.json"):
                stream = archive.extractfile(path)
                if stream is None:
                    raise ValueError("missing shard manifest")
                cache_manifests.append(json.load(stream))
        identities = [m["identity"] for m in cache_manifests]
        if sorted(m["shard"] for m in identities) != list(range(6)):
            raise ValueError("missing or duplicate shard identity")
        if any(
            m["transcripts_sha256"] != UNCOMPRESSED_SHA256 or m["shards"] != 6 for m in identities
        ):
            raise ValueError("shard extraction identity mismatch")
        if any(m["stage"] != "complete" or m["errors"] != 0 for m in cache_manifests):
            raise ValueError("incomplete extraction cache")
        archived_files = len(members)
        archived_bytes = sum(m.size for m in members.values())
    if publisher_manifest["observations"] != EXPECTED["observations"]:
        raise ValueError("publisher manifest observation count mismatch")
    readonly = all(path.stat().st_mode & 0o222 == 0 for path in [root, *root.rglob("*")])
    if not readonly:
        raise ValueError("bundle is not locked read-only")
    source_files = manifest["huggingface"]["files"] + len(companion["files"])
    return {
        "source_files": source_files,
        "source_bytes": manifest["huggingface"]["total_bytes"] + companion["total_bytes"],
        "huggingface_files": manifest["huggingface"]["files"],
        "huggingface_bytes": manifest["huggingface"]["total_bytes"],
        "companion_files": len(companion["files"]),
        "companion_bytes": companion["total_bytes"],
        "manifest_verified_entries": len(manifest["files"]),
        "manifest_verified_bytes": verified_bytes,
        "archive_members": archived_files,
        "archive_uncompressed_bytes": archived_bytes,
        "publisher_member_digests_verified": len(publisher_manifest["files"]),
        "remaining_archive_members_authenticated_by_archive_digest": archived_files
        - len(publisher_manifest["files"]),
        "transcript_uncompressed_bytes": len(uncompressed),
        "shards": len(identities),
        "shard_expected_rows": sum(m["n_expected"] for m in identities),
        "model_revisions": dict(Counter(m["revision"] for m in identities)),
        "locked_readonly": readonly,
        "data_sha256": {
            ARCHIVE: ARCHIVE_SHA256,
            TRANSCRIPTS: TRANSCRIPTS_SHA256,
            "extraction_input_uncompressed": UNCOMPRESSED_SHA256,
        },
        "staged_files": [
            {
                "path": item["path"],
                "bytes": item["bytes"],
                "sha256": item.get("sha256") or item["sha256_computed"],
            }
            for item in manifest["files"]
        ],
    }


def profile(root: Path) -> dict[str, Any]:
    integrity = verify_bundle(root)
    rows = list(joined_rows(root))
    ids = [row["example_id"] for row in rows]
    tasks: dict[str, Counter[str]] = defaultdict(Counter)
    episodes = Counter(row["outcome"]["episode_id"] for row in rows)
    for row in rows:
        outcome = row["outcome"]
        task = tasks[hashed_id(outcome["problem"])]
        task["rows"] += 1
        if outcome["verdict"] == "HACK":
            task["hack"] += 1
            task[outcome["strength"].lower()] += 1
        elif outcome["verdict"] == "unflagged":
            task["weak_negative"] += 1
        else:
            raise ValueError("unknown verdict")
    counts = {
        "observations": len(rows),
        "hack": sum(r["outcome"]["verdict"] == "HACK" for r in rows),
        "strong": sum(r["outcome"].get("strength") == "STRONG" for r in rows),
        "weak": sum(r["outcome"].get("strength") == "WEAK" for r in rows),
        "unflagged": sum(r["outcome"]["verdict"] == "unflagged" for r in rows),
        "problems": len(tasks),
        "episodes": len(episodes),
        "shards": integrity["shards"],
    }
    if counts != EXPECTED or integrity["shard_expected_rows"] != len(rows):
        raise ValueError("observed counts differ from publisher counts")
    if len(set(ids)) != len(ids) or len({hashed_id(i) for i in ids}) != len(ids):
        raise ValueError("duplicate record id or hashed-id collision")
    if len({r["outcome"]["problem"] for r in rows}) != len(tasks):
        raise ValueError("problem hash collision")
    return {
        "corpus": CORPUS,
        "hf_revision": HF_REVISION,
        "companion_commit": COMPANION_COMMIT,
        "licence": "mit",
        "use": "local_only_cross_test_only",
        "counts": {
            **counts,
            "positives": counts["hack"],
            "weak_negatives": counts["unflagged"],
            "unlabeled": 0,
            "confirmed_clean_negatives": 0,
        },
        "publisher_count_checks": {key: counts[key] == value for key, value in EXPECTED.items()},
        "join": {
            "fields": ["example_id", "turn"],
            "matched": len(rows),
            "unmatched": 0,
            "outcome_mismatches": 0,
            "label_unit": "selected_turn_not_episode",
        },
        "id_uniqueness": {
            "record_ids": len(set(ids)),
            "join_keys": len({(r["example_id"], r["turn"]) for r in rows}),
            "trace_keys": len({hashed_id(i) for i in ids}),
            "duplicates": 0,
        },
        "sorted_record_ids_sha256": hashlib.sha256(
            ("\n".join(sorted(ids)) + "\n").encode()
        ).hexdigest(),
        "id_digest_recipe": "UTF-8, sorted example_id values, one per line, including final newline",
        "verdict": dict(Counter(r["outcome"]["verdict"] for r in rows)),
        "strength": dict(Counter(r["outcome"].get("strength") or "not_applicable" for r in rows)),
        "label": dict(Counter(r["outcome"]["label"] for r in rows)),
        "harness_label": dict(Counter(r["outcome"]["harness_label"] for r in rows)),
        "model": dict(Counter(r["outcome"]["model_key"] for r in rows)),
        "generator_scenario": dict(Counter(r["scenario"] for r in rows)),
        "turn": dict(Counter(str(r["turn"]) for r in rows)),
        "truncated": sum(r["truncated"] for r in rows),
        "episode_observation_multiplicity": dict(Counter(str(n) for n in episodes.values())),
        "per_problem_hashed": {key: dict(value) for key, value in sorted(tasks.items())},
        "message_roles": dict(Counter(m["role"] for r in rows for m in r["messages"])),
        "integrity": integrity,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--output", type=Path, default=Path(__file__).resolve().parents[1] / "results/profile.json"
    )
    args = parser.parse_args()
    result = profile(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "counts": result["counts"],
                "join": result["join"],
                "sorted_record_ids_sha256": result["sorted_record_ids_sha256"],
                "source_files": result["integrity"]["source_files"],
                "source_bytes": result["integrity"]["source_bytes"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
