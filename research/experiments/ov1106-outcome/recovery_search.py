#!/usr/bin/env python3
"""Bounded exact-identity recovery of the authored Astra task package.

Only copies under --raw are changed. No grading or container execution occurs.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

from evallab.registry import harbor_task_digest, task_directory_digest

EXPECTED = "sha256:ea04e3a7e37d905d5cb92e65b53b25b50cca65625fdab6f9150ffd6dd7ece0a5"
PACKAGE = (
    "research/experiments/ov-safeguard-friction-v2-20261031/tasks/astra-strict-schema-friction"
)
REVISIONS = ["0d6420271e8151b7a67d4084d1a55be4711dfdc5", "0cf63af38e23b45ba20bea438ddf26ea10053f05"]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root)


def inventory(path):
    return [
        {
            "path": p.relative_to(path).as_posix(),
            "sha256": sha(p.read_bytes()),
            "mode": oct(p.stat().st_mode & 0o777),
            "size": p.stat().st_size,
        }
        for p in sorted(path.rglob("*"))
        if p.is_file()
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--snapshot", action="append", type=Path, default=[])
    args = parser.parse_args()
    args.raw.mkdir(parents=True, exist_ok=False)
    candidates = []
    sources = []

    def record(label, path, provenance):
        entry = {
            "label": label,
            "path": str(path.resolve()),
            "provenance": provenance,
            "digest": task_directory_digest(path),
            "harbor_digest": harbor_task_digest(path),
            "files": inventory(path),
        }
        entry["identity_matches"] = entry["digest"] == EXPECTED
        candidates.append(entry)
        return entry

    current = args.repo / PACKAGE
    record("current_package", current, "Present authored package; read only")
    restored = Path("/private/tmp/ov1106/outcome/coverage/recovery-audit/restored-package")
    record("prior_baseline_restored", restored, "Prior recovery copy; read only")
    unavailable_snapshots = []
    for index, snapshot in enumerate(args.snapshot):
        if not snapshot.is_dir():
            unavailable_snapshots.append(str(snapshot))
            continue
        target = args.raw / f"snapshot-{index:02d}"
        shutil.copytree(snapshot, target)
        record(f"retained_snapshot_{index:02d}", target, str(snapshot.resolve()))
    workspace = Path("/private/tmp/w3sf/work/astra-strict-schema-friction__dsv4pro__t1/testbed")
    baseline = {
        rel: git(workspace, "show", "HEAD:" + rel)
        for rel in git(workspace, "ls-tree", "-r", "--name-only", "HEAD").decode().splitlines()
    }
    for revision in REVISIONS:
        target = args.raw / ("git-" + revision[:12])
        archive = git(args.repo, "archive", revision, PACKAGE)
        sources.append(
            {
                "revision": revision,
                "archive_sha256": sha(archive),
                "tree": git(args.repo, "rev-parse", revision + ":" + PACKAGE).decode().strip(),
            }
        )
        target.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                rel = Path(member.name).relative_to(PACKAGE)
                destination = target / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(tar.extractfile(member).read())
                destination.chmod(member.mode)
        record("git_package_" + revision[:12], target, revision + ":" + PACKAGE)
        staged = args.raw / ("git-baseline-" + revision[:12])
        shutil.copytree(target, staged)
        changes = []
        for rel, data in baseline.items():
            for prefix in ("environment/app", "tests/pristine"):
                path = staged / prefix / rel
                if path.is_file() and path.read_bytes() != data:
                    changes.append(path.relative_to(staged).as_posix())
                    path.write_bytes(data)
        record(
            "git_baseline_" + revision[:12],
            staged,
            {"revision": revision, "baseline_restored": changes},
        )

    # The digest ignores cache filenames, not ancestor directories. Check retained
    # pytest-cache bytes explicitly instead of assuming all caches are excluded.
    cache = workspace / ".pytest_cache"
    cache_candidates = []
    if cache.is_dir():
        for index, prefix in enumerate(("", "environment/app", "tests", "tests/pristine")):
            staged = args.raw / f"retained-pytest-cache-{index}"
            shutil.copytree(restored, staged)
            shutil.copytree(cache, staged / prefix / ".pytest_cache")
            entry = record(
                f"retained_pytest_cache_{index}",
                staged,
                {
                    "baseline": str(restored),
                    "cache_source": str(cache),
                    "cache_destination": prefix + "/.pytest_cache",
                },
            )
            cache_candidates.append(entry["digest"])

    semantics = {
        "algorithm": "sha256 of sorted '<file-sha256>  ./<relative-path>\\n' records",
        "file_modes_and_timestamps_included": False,
        "ignored_basenames": [".DS_Store", ".git", "__pycache__", ".pytest_cache"],
        "ignored_suffixes": [".pyc", ".pyo", ".tmp"],
        "hidden_files_otherwise_included": True,
        "ignored_ancestor_directories": False,
        "original_harbor_lock_digest": "sha256:9831a2c65cdfe933c45cfb758398bc16b9c29f58ddd5febd22167aaf9bc6f3a2",
        "harbor_lock_matches": [
            c["path"]
            for c in candidates
            if c["harbor_digest"]
            == "sha256:9831a2c65cdfe933c45cfb758398bc16b9c29f58ddd5febd22167aaf9bc6f3a2"
        ],
        "retained_cache_candidate_digests": cache_candidates,
    }
    provenance = []
    for name in ("grade.json",):
        path = Path("/private/tmp/w3sf/out/astra-strict-schema-friction__dsv4pro__t1") / name
        provenance.append({"path": str(path), "sha256": sha(path.read_bytes())})
    trial = Path("/private/tmp/w3sf/trials/astra-strict-schema-friction__dsv4pro__t1/trial")
    for name in ("config.json", "lock.json", "qa-execution.json"):
        path = trial / name
        provenance.append({"path": str(path), "sha256": sha(path.read_bytes())})
    result = {
        "expected_digest": EXPECTED,
        "candidates": candidates,
        "git_sources": sources,
        "unavailable_snapshots": unavailable_snapshots,
        "original_provenance": provenance,
        "digest_semantics": semantics,
        "script_sha256": sha(Path(__file__).read_bytes()),
        "recovered": [c["path"] for c in candidates if c["identity_matches"]],
        "new_container_grades": 0,
        "additional_admitted_rows": 0,
    }
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "recovered": result["recovered"],
                "candidates": [
                    {k: c[k] for k in ("label", "digest", "harbor_digest", "identity_matches")}
                    for c in candidates
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
