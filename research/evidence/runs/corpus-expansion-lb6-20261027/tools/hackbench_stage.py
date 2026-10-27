#!/usr/bin/env python3
"""Stage and verify facebook/hackbench as a local-only, cross-test-only bundle (LB-6).

Standard library only. The dataset is gated (CC BY-NC 4.0; users agree not to train on it and not to
rehost it), so this tool

* downloads one pinned revision into a directory outside every git checkout and outside the
  production database and content store (default ``~/.local/state/evallab-cross-test``),
* never prints, logs or stores the token (``HF_TOKEN`` is read from the environment; run it as
  ``keys run -- python hackbench_stage.py fetch``),
* verifies each file against the publisher's ``SHA256SUMS`` and ``MANIFEST.json`` and against the
  Git blob SHA-1 the Hub reports, checks the row structure, and writes an aggregate-only profile
  (counts and digests, no trajectory text),
* writes ``gold_labels.json`` (indices and metadata only, no command text) so labels stay separate
  from detector inputs.

Usage::

    keys run -- python hackbench_stage.py fetch  [--root DIR]
    python hackbench_stage.py verify [--root DIR] [--profile OUT.json]
    python hackbench_stage.py labels [--root DIR]
    python hackbench_stage.py lock   [--root DIR]        # read-only permissions
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import sys
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

REPO = "facebook/hackbench"
REVISION = "b3d6f678e6359dcd35e20f3f2c5ee085b5af4934"
DATA = "data/hackbench_169_gpt55_glm52.numbered.jsonl"
FILES = (
    ".gitattributes",
    "MANIFEST.json",
    "README.md",
    "SHA256SUMS",
    "schemas/example.schema.json",
    DATA,
)
#: Git blob SHA-1 of each small file at REVISION, as the Hub API reports it (``blobId``).
BLOB_IDS = {
    ".gitattributes": "4030271851b0e7764fd603c9941753dbf5bff82d",
    "MANIFEST.json": "1cc31565dfdfff9f60e74ee36e8f7d95b6af2739",
    "README.md": "659b1df2aaed430af7d01ac14168b720dbf120e5",
    "SHA256SUMS": "25b0451e68b2bac365cab08eeb8d6e5580a9872b",
    "schemas/example.schema.json": "e5de0295a2a46b8a661ac98e6b94f83acbd9ede4",
}
#: LFS SHA-256 of the data file at REVISION (the Hub's ``lfs.sha256``).
DATA_SHA256 = "42c0cfa4a3dc822eb6505fbcc58989c5df5dcf509cc4a97338abbb38c7e9fd87"
DATA_BYTES = 408_528_473
DEFAULT_ROOT = Path.home() / ".local/state/evallab-cross-test/meta-hackbench" / REVISION[:12]
CMD_RE = re.compile(r"\[CMD (\d+)\]")
CMD_HEADER_RE = re.compile(r"\[CMD (\d+)\] \[(?:command|tool: ([^\]]*))\]")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def git_blob_sha1(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def fetch(root: Path) -> None:
    token = os.environ.get("HF_TOKEN")
    if not token:
        sys.exit("HF_TOKEN is not set; run as: keys run -- python hackbench_stage.py fetch")
    for name in FILES:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.chmod(0o600)
        url = f"https://huggingface.co/datasets/{REPO}/resolve/{REVISION}/{name}"
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=120) as response, target.open("wb") as out:
            while block := response.read(1 << 20):
                out.write(block)
        print(f"fetched {name} {target.stat().st_size} bytes")
    root.parent.chmod(0o700)


def parse_sums(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        digest, _, name = line.partition("  ")
        if digest and name:
            out[name.strip()] = digest
    return out


def verify_digests(root: Path) -> dict[str, Any]:
    sums = parse_sums(root / "SHA256SUMS")
    manifest = json.loads((root / "MANIFEST.json").read_text())
    manifest_hashes = manifest["file_hashes"]
    files: dict[str, Any] = {}
    for name in FILES:
        path = root / name
        item: dict[str, Any] = {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
        if name in sums:
            item["matches_SHA256SUMS"] = item["sha256"] == sums[name]
        if name in manifest_hashes:
            item["matches_MANIFEST"] = item["sha256"] == manifest_hashes[name]
        if name in BLOB_IDS:
            item["git_blob_sha1"] = git_blob_sha1(path)
            item["matches_hub_blob_id"] = item["git_blob_sha1"] == BLOB_IDS[name]
        files[name] = item
    files[DATA]["matches_hub_lfs_sha256"] = files[DATA]["sha256"] == DATA_SHA256
    files[DATA]["matches_expected_bytes"] = files[DATA]["bytes"] == DATA_BYTES
    files["schemas/example.schema.json"]["matches_MANIFEST_schema_sha256"] = (
        files["schemas/example.schema.json"]["sha256"] == manifest["artifact"]["schema_sha256"]
    )
    return {"revision": REVISION, "files": files, "manifest": manifest}


def quantiles(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    n = len(ordered)
    return {
        "min": ordered[0],
        "p25": ordered[n // 4],
        "median": statistics.median(ordered),
        "p75": ordered[(3 * n) // 4],
        "max": ordered[-1],
    }


def profile_rows(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Structural checks and an aggregate profile. Returns ``(profile, label_rows)``; no text."""
    labels: list[dict[str, Any]] = []
    problems: list[str] = []
    seen_ids: set[str] = set()
    raw_hashes: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    gold_kinds: Counter[str] = Counter()
    with (root / DATA).open(encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            row = json.loads(line)
            for key in (
                "id",
                "task",
                "source_agent",
                "injected_transcript",
                "hack_commands",
                "monitor_input",
                "gold_command_indices",
                "origin",
                "hack_type",
            ):
                if key not in row:
                    problems.append(f"row {line_no}: missing {key}")
            ident = row["id"]
            if ident in seen_ids:
                problems.append(f"row {line_no}: duplicate id")
            seen_ids.add(ident)
            transcript = row["monitor_input"]["transcript"]
            count = row["monitor_input"]["command_count"]
            numbers = [int(n) for n in CMD_RE.findall(transcript)]
            if numbers != list(range(1, count + 1)):
                problems.append(f"row {line_no}: [CMD n] labels are not 1..command_count")
            gold = row["gold_command_indices"]
            if (
                not gold
                or gold != sorted(set(gold))
                or not all(isinstance(i, int) and 1 <= i <= count for i in gold)
            ):
                problems.append(f"row {line_no}: gold indices out of range or unsorted")
            if len(row["hack_commands"]) != len(gold):
                problems.append(f"row {line_no}: hack_commands and gold lengths differ")
            row_kinds = {
                int(m.group(1)): (m.group(2) or "command")
                for m in CMD_HEADER_RE.finditer(transcript)
            }
            if len(row_kinds) != count:
                problems.append(f"row {line_no}: a [CMD n] block has an unrecognised header")
            kinds.update(row_kinds.values())
            gold_kinds.update(row_kinds.get(i, "?") for i in gold)
            raw_hashes[hashlib.sha256(row["injected_transcript"].encode()).hexdigest()] += 1
            agent = row["source_agent"]
            labels.append(
                {
                    "id": ident,
                    "task": row["task"]["name"],
                    "origin": row["origin"],
                    "hack_type": row["hack_type"],
                    "model": agent["configured_model_name"],
                    "agent": f"{agent['agent_name']} {agent['agent_version']}",
                    "command_count": count,
                    "gold_command_indices": gold,
                    "transcript_chars": len(transcript),
                    "raw_transcript_sha256": hashlib.sha256(
                        row["injected_transcript"].encode()
                    ).hexdigest(),
                }
            )
    gold_counts = [len(r["gold_command_indices"]) for r in labels]
    cmd_counts = [r["command_count"] for r in labels]
    first_gold = [min(r["gold_command_indices"]) for r in labels]
    profile = {
        "rows": len(labels),
        "distinct_ids": len(seen_ids),
        "distinct_raw_transcripts": len(raw_hashes),
        "structural_problems": problems,
        "models": dict(Counter(r["model"] for r in labels)),
        "origins": dict(Counter(r["origin"] for r in labels)),
        "hack_types": dict(Counter(r["hack_type"] for r in labels)),
        "origin_x_hack_type": {
            f"{o}/{h}": n
            for (o, h), n in Counter((r["origin"], r["hack_type"]) for r in labels).items()
        },
        "tasks": dict(sorted(Counter(r["task"] for r in labels).items())),
        "agents": dict(Counter(r["agent"] for r in labels)),
        "command_blocks_total": sum(cmd_counts),
        "gold_commands_total": sum(gold_counts),
        "gold_density": round(sum(gold_counts) / sum(cmd_counts), 4),
        "commands_per_trajectory": quantiles(cmd_counts),
        "gold_per_trajectory": quantiles(gold_counts),
        "first_gold_index": quantiles(first_gold),
        "transcript_chars": quantiles([r["transcript_chars"] for r in labels]),
        "command_block_kinds": dict(kinds),
        "gold_block_kinds": dict(gold_kinds),
        "by_origin": {
            origin: {
                "rows": sum(r["origin"] == origin for r in labels),
                "gold_commands": sum(
                    len(r["gold_command_indices"]) for r in labels if r["origin"] == origin
                ),
                "command_blocks": sum(r["command_count"] for r in labels if r["origin"] == origin),
            }
            for origin in sorted({r["origin"] for r in labels})
        },
        "ids_sha256": hashlib.sha256("\n".join(sorted(seen_ids)).encode()).hexdigest(),
    }
    return profile, labels


def write_labels(root: Path, labels: list[dict[str, Any]]) -> Path:
    path = root / "gold_labels.json"
    if path.exists():
        path.chmod(0o600)
    path.write_text(json.dumps({"source": f"{REPO}@{REVISION}", "rows": labels}, indent=1) + "\n")
    path.chmod(0o400)
    return path


def lock(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        path.chmod(0o500 if path.is_dir() else 0o400)
    root.chmod(0o500)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=("fetch", "verify", "labels", "lock"))
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--profile", type=Path)
    args = parser.parse_args()
    if args.command == "fetch":
        fetch(args.root)
    elif args.command == "verify":
        digests = verify_digests(args.root)
        profile, _ = profile_rows(args.root)
        manifest = digests.pop("manifest")
        checks = {
            "rows_equal_manifest": profile["rows"] == manifest["rows"],
            "models_equal_manifest": profile["models"] == manifest["counts"]["models"],
            "tasks_equal_manifest": profile["tasks"] == manifest["counts"]["tasks"],
            "origins_equal_manifest": profile["origins"] == manifest["counts"]["origins"],
            "distinct_raw_equal_manifest": profile["distinct_raw_transcripts"]
            == manifest["distinct_raw_transcripts"],
            "gold_total_equal_manifest": profile["gold_commands_total"]
            == manifest["numbering"]["gold_command_indices_count"],
            "command_counts_equal_manifest": all(
                profile["commands_per_trajectory"][k] == manifest["command_counts"][k]
                for k in ("min", "median", "max")
            ),
        }
        out = {
            "source": f"{REPO}@{REVISION}",
            "digests": digests["files"],
            "manifest_checks": checks,
            "profile": profile,
        }
        text = json.dumps(out, indent=1, sort_keys=True)
        if args.profile:
            args.profile.write_text(text + "\n")
        print(text)
        bad = [k for k, v in checks.items() if not v] + profile["structural_problems"]
        if bad:
            print("PROBLEMS:", bad, file=sys.stderr)
    elif args.command == "labels":
        _, labels = profile_rows(args.root)
        print(write_labels(args.root, labels))
    else:
        lock(args.root)
        print("locked read-only", args.root)


if __name__ == "__main__":
    main()
