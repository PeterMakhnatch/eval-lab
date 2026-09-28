"""Sealed train/held-out task splits for distillation datasets (HAR-81).

A teacher/student distillation run needs one frozen split that both the SFT
export (:mod:`evallab.sft_terminus`) and the later held-out re-evaluation
agree on. This module freezes that split over a directory of Harbor task
packages (e.g. a downloaded MiMo-V2.6-RL dataset ``tasks/`` tree).

Determinism: within each domain, task ids are ranked by
``sha256(salt + "\\0" + task_id)`` and the first ``heldout_count`` ids (by
rank) are sealed as ``heldout``; every other task is ``train``. The same
salt, source dataset, and task set always reproduce byte-identical
manifests, which carry no wall-clock values.

The manifest records each task's package digest
(:func:`evallab.evidence_store.evidence_tree_digest`), the source dataset
and revision it was frozen from, and its own ``manifest_digest`` over the
canonical JSON of every other field. The exporter verifies that digest on
load, so a silently edited split cannot relabel a run.

This is a dataset-level split keyed by *task id*, sealed before any teacher
run; :func:`evallab.sft_records.load_split_manifest` remains the separate
trial-family assignment consumed by the HAR-65 record bridge.

Run with ``python -m evallab.sft_split freeze --root DOMAIN=PATH ...
--salt S --source-dataset DS --source-revision REV --heldout-count D=N
--out split.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.evidence_store import evidence_tree_digest
from evallab.tracing import TraceError

CONTRACT_VERSION = "evallab.sft_split/1"
MANIFEST_DIGEST_KEY = "manifest_digest"
#: A Harbor task package is a directory carrying ``task.toml`` at its root.
TASK_MARKER = "task.toml"
ASSIGNMENTS = ("train", "heldout")


@dataclass(frozen=True)
class DomainRoot:
    """One dataset domain (e.g. ``code``) rooted at a directory of tasks."""

    domain: str
    path: Path


def _parse_domain_root(value: str) -> DomainRoot:
    domain, sep, path = value.partition("=")
    if not sep:
        path, domain = value, Path(value).name
    domain = domain.strip()
    if not domain or "/" in domain or domain in {".", ".."}:
        raise TraceError(f"domain label must be a plain directory name: {value!r}")
    return DomainRoot(domain=domain, path=Path(path))


def discover_task_packages(root: Path) -> list[Path]:
    """Sorted immediate subdirectories of ``root`` that are task packages."""
    if not root.is_dir():
        raise TraceError(f"domain root is not a directory: {root.resolve()}")
    found = [child for child in sorted(root.iterdir()) if (child / TASK_MARKER).is_file()]
    if not found:
        raise TraceError(f"no task packages ({TASK_MARKER}) under {root.resolve()}")
    return found


def rank_key(salt: str, task_id: str) -> str:
    """The deterministic hash ranking a task inside its domain."""
    return hashlib.sha256(f"{salt}\0{task_id}".encode()).hexdigest()


def _heldout_count_for(
    domain: str, total: int, counts: dict[str, int], fraction: float | None
) -> int:
    if domain in counts:
        explicit = counts[domain]
    elif fraction is not None:
        explicit = int(math.floor(fraction * total + 0.5))
    else:
        raise TraceError(
            f"domain {domain!r} has no explicit held-out count or fraction; "
            "a sealed split must state every domain's held-out size "
            "(--heldout-count DOMAIN=N or --heldout-fraction)"
        )
    if not 0 <= explicit <= total:
        raise TraceError(
            f"held-out count for domain {domain!r} is {explicit}, "
            f"outside 0..{total}"
        )
    return explicit


def build_split(
    roots: list[DomainRoot],
    *,
    salt: str,
    source_dataset: str,
    source_revision: str,
    heldout_counts: dict[str, int],
    heldout_fraction: float | None,
) -> dict[str, Any]:
    """Freeze the split manifest for every domain root (never writes files)."""
    if not salt:
        raise TraceError("salt must be a nonempty string")
    if not source_dataset:
        raise TraceError("source_dataset must name the dataset the tasks came from")
    if not source_revision:
        raise TraceError(
            "source_revision must pin the dataset revision (commit sha or tag)"
        )
    seen: dict[str, str] = {}
    domains: dict[str, Any] = {}
    for root in roots:
        tasks: list[dict[str, Any]] = []
        for package in discover_task_packages(root.path):
            task_id = package.name
            if task_id in seen:
                raise TraceError(
                    f"task id {task_id!r} appears in both domains "
                    f"{seen[task_id]!r} and {root.domain!r}; held-out refusal "
                    "requires globally unique task ids"
                )
            seen[task_id] = root.domain
            tasks.append(
                {
                    "task_id": task_id,
                    "package_digest": evidence_tree_digest(package),
                }
            )
        total = len(tasks)
        heldout_n = _heldout_count_for(root.domain, total, heldout_counts, heldout_fraction)
        ranked = sorted(tasks, key=lambda task: (rank_key(salt, task["task_id"]), task["task_id"]))
        for position, task in enumerate(ranked):
            task["assignment"] = "heldout" if position < heldout_n else "train"
        domains[root.domain] = {
            "path": root.path.as_posix(),
            "task_count": total,
            "heldout_count": heldout_n,
            "tasks": sorted(tasks, key=lambda task: task["task_id"]),
        }
    manifest: dict[str, Any] = {
        "contract": CONTRACT_VERSION,
        "salt": salt,
        "source_dataset": source_dataset,
        "source_revision": source_revision,
        "heldout_fraction": heldout_fraction,
        "domains": domains,
        "counts": {
            "train": sum(
                1 for tasks in domains.values() for task in tasks["tasks"]
                if task["assignment"] == "train"
            ),
            "heldout": sum(
                1 for tasks in domains.values() for task in tasks["tasks"]
                if task["assignment"] == "heldout"
            ),
        },
    }
    manifest["heldout_task_ids"] = sorted(
        task["task_id"]
        for tasks in domains.values()
        for task in tasks["tasks"]
        if task["assignment"] == "heldout"
    )
    manifest[MANIFEST_DIGEST_KEY] = split_digest(manifest)
    return manifest


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def split_digest(manifest: dict[str, Any]) -> str:
    """Digest over every manifest field except ``manifest_digest`` itself."""
    payload = {key: value for key, value in manifest.items() if key != MANIFEST_DIGEST_KEY}
    return f"sha256:{hashlib.sha256(_canonical_bytes(payload)).hexdigest()}"


def write_split(manifest: dict[str, Any], path: Path, *, force: bool = False) -> None:
    """Write the sealed manifest; an existing file is never replaced silently."""
    if path.exists() and not force:
        raise TraceError(f"split manifest already exists (sealed): {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def load_split(path: Path) -> dict[str, Any]:
    """Load and digest-verify a sealed split manifest (fails closed)."""
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise TraceError(f"split manifest is unreadable: {path} ({exc})") from exc
    if not isinstance(loaded, dict) or loaded.get("contract") != CONTRACT_VERSION:
        raise TraceError(f"not a {CONTRACT_VERSION} split manifest: {path}")
    recorded = loaded.get(MANIFEST_DIGEST_KEY)
    actual = split_digest(loaded)
    if recorded != actual:
        raise TraceError(
            f"split manifest digest mismatch for {path}: recorded {recorded}, "
            f"computed {actual}; a sealed split must never be edited"
        )
    return loaded


def heldout_task_ids(manifest: dict[str, Any]) -> set[str]:
    return set(manifest.get("heldout_task_ids") or ())


def _parse_heldout(value: str) -> tuple[str, int]:
    domain, sep, count = value.partition("=")
    if not sep:
        raise TraceError(f"--heldout-count must be DOMAIN=N, got {value!r}")
    try:
        number = int(count)
    except ValueError as exc:
        raise TraceError(f"--heldout-count must be DOMAIN=N, got {value!r}") from exc
    return domain, number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("freeze", help="freeze a sealed train/held-out split")
    freeze.add_argument(
        "--root",
        action="append",
        required=True,
        type=_parse_domain_root,
        help="DOMAIN=PATH directory of task packages; repeatable",
    )
    freeze.add_argument("--salt", required=True, help="sealing salt (any nonempty string)")
    freeze.add_argument("--source-dataset", required=True, help="e.g. FineEnvs/MiMo-V2.6-RL-harbor")
    freeze.add_argument(
        "--source-revision",
        required=True,
        help="pinned dataset revision (commit sha or tag)",
    )
    freeze.add_argument(
        "--heldout-count",
        action="append",
        default=[],
        type=_parse_heldout,
        help="DOMAIN=N explicit held-out task count; repeatable",
    )
    freeze.add_argument(
        "--heldout-fraction",
        type=float,
        default=None,
        help="default per-domain held-out fraction for domains without an explicit count",
    )
    freeze.add_argument("--out", type=Path, required=True, help="output manifest path (new file)")
    freeze.add_argument(
        "--force", action="store_true", help="replace an existing manifest at --out"
    )
    args = parser.parse_args(argv)
    domains = [root.domain for root in args.root]
    duplicates = sorted({domain for domain in domains if domains.count(domain) > 1})
    if duplicates:
        print(f"error: duplicate domain roots: {', '.join(duplicates)}")
        return 2
    try:
        manifest = build_split(
            args.root,
            salt=args.salt,
            source_dataset=args.source_dataset,
            source_revision=args.source_revision,
            heldout_counts=dict(args.heldout_count),
            heldout_fraction=args.heldout_fraction,
        )
        write_split(manifest, args.out, force=args.force)
    except TraceError as exc:
        print(f"error: {exc}")
        return 2
    print(
        json.dumps(
            {
                "out": args.out.as_posix(),
                "manifest_digest": manifest[MANIFEST_DIGEST_KEY],
                "counts": manifest["counts"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
