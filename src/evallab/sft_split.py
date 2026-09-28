"""Sealed train/held-out task splits for the MiMo catalog (HAR-81/HAR-84).

A teacher/student distillation run needs one frozen split that the SFT export
(:mod:`evallab.sft_terminus`), the Tinker training manifest, and the later
held-out re-evaluation all agree on. The split is frozen from the shared
MiMo task catalog (``task_versions.parquet``, built by
``evallab tasks catalog build``; see ``docs/mimo-task-catalog.md``) — never
from an ad hoc directory walk — and buckets tasks on ``split_group``, the
catalog's stable family key, so sibling tasks never straddle train and
held-out.

Determinism: within each domain, split groups are ranked by
``sha256(salt + "\\0" + split_group)`` and whole groups are taken into
held-out in rank order until the held-out task count reaches the requested
per-domain count (the actual count is reported; a group is never split).
The same salt and catalog table always reproduce byte-identical manifests,
which carry no wall-clock values.

The manifest records the catalog table digest it was frozen from, each
domain's pinned ``source_repo@source_revision``, a per-task list keyed by
``task_version_digest`` (:func:`evallab.registry.task_directory_digest` —
the catalog's scheme, reused, not duplicated), a top-level ``splits`` map
consumed unchanged by ``evallab tasks catalog export-eligible --split`` (
:func:`evallab.task_catalog._read_split_map`), and its own
``manifest_digest`` over the canonical JSON of every other field, verified
on load so a silently edited split cannot relabel a run.

Run with ``python -m evallab.sft_split freeze --salt S
--heldout-count DOMAIN=N ... --out split.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evallab.tracing import TraceError

CONTRACT_VERSION = "evallab.sft_split/2"
MANIFEST_DIGEST_KEY = "manifest_digest"
CATALOG_TABLE_FILENAME = "task_versions.parquet"
GROUPING_KEY = "split_group"
ASSIGNMENTS = ("train", "heldout")

#: Columns the split needs from the catalog ``task_versions`` table.
REQUIRED_COLUMNS = (
    "domain",
    "task_id",
    "task_name",
    "split_group",
    "task_version_digest",
    "source_repo",
    "source_revision",
)

_DIGEST_SHAPE = "sha256:" + "0" * 64


@dataclass(frozen=True)
class CatalogTask:
    """One ``task_versions`` row the split is frozen over."""

    domain: str
    task_id: str
    task_name: str
    split_group: str
    task_version_digest: str
    source_repo: str
    source_revision: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def load_catalog_tasks(table: Path) -> list[CatalogTask]:
    """Read and validate the ``task_versions`` parquet into split rows."""
    import pyarrow.parquet as pq

    if not table.is_file():
        raise TraceError(f"catalog table not found: {table.resolve()}")
    loaded = pq.read_table(table)
    missing = [name for name in REQUIRED_COLUMNS if name not in loaded.column_names]
    if missing:
        raise TraceError(f"catalog table {table} lacks required columns: {missing}")
    tasks: list[CatalogTask] = []
    seen_ids: dict[str, str] = {}
    seen_digests: set[str] = set()
    for row in loaded.select(list(REQUIRED_COLUMNS)).to_pylist():
        task = CatalogTask(
            domain=str(row["domain"]),
            task_id=str(row["task_id"]),
            task_name=str(row["task_name"]),
            split_group=str(row["split_group"]),
            task_version_digest=str(row["task_version_digest"]),
            source_repo=str(row["source_repo"]),
            source_revision=str(row["source_revision"]),
        )
        for field in ("domain", "task_id", "task_name", "split_group"):
            if not getattr(task, field).strip():
                raise TraceError(f"catalog row for {task.task_id!r} has an empty {field}")
        if len(task.task_version_digest) != len(_DIGEST_SHAPE) or not (
            task.task_version_digest.startswith("sha256:")
            and all(c in "0123456789abcdef" for c in task.task_version_digest[7:])
        ):
            raise TraceError(
                f"catalog row for {task.task_id!r} has a malformed "
                f"task_version_digest: {task.task_version_digest!r}"
            )
        if task.task_id in seen_ids:
            raise TraceError(
                f"task id {task.task_id!r} appears in both domains "
                f"{seen_ids[task.task_id]!r} and {task.domain!r}; held-out "
                "refusal requires globally unique task ids"
            )
        if task.task_version_digest in seen_digests:
            raise TraceError(
                f"task_version_digest {task.task_version_digest} is shared by "
                "more than one catalog row"
            )
        seen_ids[task.task_id] = task.domain
        seen_digests.add(task.task_version_digest)
        tasks.append(task)
    if not tasks:
        raise TraceError(f"catalog table {table} contains no task rows")
    return tasks


def group_rank_key(salt: str, split_group: str) -> str:
    """The deterministic hash ranking a split group inside its domain."""
    return hashlib.sha256(f"{salt}\0{split_group}".encode()).hexdigest()


def _heldout_requested(domain: str, heldout_counts: dict[str, int]) -> int:
    if domain not in heldout_counts:
        raise TraceError(
            f"domain {domain!r} has no held-out count; a sealed split must "
            "state every catalog domain's held-out size (--heldout-count "
            "DOMAIN=N)"
        )
    return heldout_counts[domain]


def build_split(
    tasks: list[CatalogTask],
    *,
    salt: str,
    catalog_table: str,
    catalog_digest: str,
    catalog_rows: int,
    heldout_counts: dict[str, int],
) -> dict[str, Any]:
    """Freeze the split manifest over catalog rows (never writes files)."""
    if not salt:
        raise TraceError("salt must be a nonempty string")
    if not catalog_digest.startswith("sha256:"):
        raise TraceError("catalog_digest must be a sha256: digest of the table bytes")

    domains = sorted({task.domain for task in tasks})
    unknown = sorted(set(heldout_counts) - set(domains))
    if unknown:
        raise TraceError(
            "held-out counts name domains absent from the catalog: " + ", ".join(unknown)
        )

    manifest_tasks: list[dict[str, Any]] = []
    domain_blocks: dict[str, Any] = {}
    for domain in domains:
        rows = [task for task in tasks if task.domain == domain]
        total = len(rows)
        requested = _heldout_requested(domain, heldout_counts)
        if not 0 <= requested <= total:
            raise TraceError(
                f"held-out count for domain {domain!r} is {requested}, outside 0..{total}"
            )
        groups: dict[str, list[CatalogTask]] = {}
        for row in rows:
            groups.setdefault(row.split_group, []).append(row)
        ranked_groups = sorted(groups, key=lambda group: (group_rank_key(salt, group), group))
        heldout: set[str] = set()
        taken = 0
        taken_groups = 0
        for group in ranked_groups:
            if taken >= requested:
                break
            heldout.add(group)
            taken += len(groups[group])
            taken_groups += 1
        sources = {row.source_repo for row in rows}
        revisions = {row.source_revision for row in rows}
        if len(sources) != 1 or len(revisions) != 1:
            raise TraceError(
                f"domain {domain!r} spans multiple pinned sources: "
                f"{sorted(sources)}@{sorted(revisions)}"
            )
        source_repo = sources.pop()
        source_revision = revisions.pop()
        domain_rows: list[dict[str, Any]] = []
        for row in rows:
            assignment = "heldout" if row.split_group in heldout else "train"
            domain_rows.append(
                {
                    "domain": domain,
                    "task_id": row.task_id,
                    "task_name": row.task_name,
                    "split_group": row.split_group,
                    "task_version_digest": row.task_version_digest,
                    "split": assignment,
                }
            )
        domain_rows.sort(key=lambda entry: entry["task_id"])
        manifest_tasks.extend(domain_rows)
        domain_blocks[domain] = {
            "source_repo": source_repo,
            "source_revision": source_revision,
            "task_count": total,
            "split_group_count": len(groups),
            "heldout": {
                "requested": requested,
                "actual": taken,
                "groups": taken_groups,
            },
            "train_task_ids": [
                entry["task_id"] for entry in domain_rows if entry["split"] == "train"
            ],
            "heldout_task_ids": [
                entry["task_id"] for entry in domain_rows if entry["split"] == "heldout"
            ],
        }

    manifest_tasks.sort(key=lambda entry: (entry["domain"], entry["task_id"]))
    manifest: dict[str, Any] = {
        "contract": CONTRACT_VERSION,
        "salt": salt,
        "grouping": GROUPING_KEY,
        "catalog": {
            "table": catalog_table,
            "digest": catalog_digest,
            "rows": catalog_rows,
        },
        "sources": {
            domain: f"{block['source_repo']}@{block['source_revision']}"
            for domain, block in sorted(domain_blocks.items())
        },
        "domains": domain_blocks,
        "tasks": manifest_tasks,
        # Consumed unchanged by task_catalog._read_split_map (export-eligible).
        "splits": {entry["task_version_digest"]: entry["split"] for entry in manifest_tasks},
        "counts": {
            "train": sum(1 for entry in manifest_tasks if entry["split"] == "train"),
            "heldout": sum(1 for entry in manifest_tasks if entry["split"] == "heldout"),
        },
    }
    manifest["heldout_task_ids"] = sorted(
        entry["task_id"] for entry in manifest_tasks if entry["split"] == "heldout"
    )
    manifest[MANIFEST_DIGEST_KEY] = split_digest(manifest)
    return manifest


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


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
    """Task ids sealed as held-out (any domain)."""
    return set(manifest.get("heldout_task_ids") or ())


def default_catalog_table(repo_root: Path) -> Path:
    """The shared catalog table path (primary checkout, never a worktree)."""
    from evallab.storage.paths import derived_root_from_environment
    from evallab.task_catalog import catalog_dir

    derived = derived_root_from_environment(repo_root)
    return catalog_dir(derived) / CATALOG_TABLE_FILENAME


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
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("freeze", help="freeze a sealed train/held-out split")
    freeze.add_argument("--salt", required=True, help="sealing salt (any nonempty string)")
    freeze.add_argument(
        "--heldout-count",
        action="append",
        default=[],
        type=_parse_heldout,
        help="DOMAIN=N requested held-out task count (whole groups; repeatable)",
    )
    freeze.add_argument("--out", type=Path, required=True, help="output manifest path (new file)")
    freeze.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help=f"task_versions.parquet path (default: shared {CATALOG_TABLE_FILENAME})",
    )
    freeze.add_argument("--derived-root", type=Path, default=None, help="derived Parquet root")
    freeze.add_argument(
        "--force", action="store_true", help="replace an existing manifest at --out"
    )
    args = parser.parse_args(argv)

    counts: dict[str, int] = {}
    for domain, number in args.heldout_count:
        if domain in counts:
            print(f"error: duplicate --heldout-count for domain {domain!r}")
            return 2
        counts[domain] = number

    try:
        if args.catalog is not None:
            table = args.catalog
        elif args.derived_root is not None:
            table = args.derived_root / "external" / "task_catalog" / CATALOG_TABLE_FILENAME
        else:
            table = default_catalog_table(repo_root)
        tasks = load_catalog_tasks(table)
        manifest = build_split(
            tasks,
            salt=args.salt,
            catalog_table=table.resolve().as_posix(),
            catalog_digest=_sha256_file(table),
            catalog_rows=len(tasks),
            heldout_counts=counts,
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
                "catalog": manifest["catalog"],
                "counts": manifest["counts"],
                "domains": {
                    domain: {
                        "requested": block["heldout"]["requested"],
                        "actual": block["heldout"]["actual"],
                        "groups": block["heldout"]["groups"],
                        "split_groups": block["split_group_count"],
                        "tasks": block["task_count"],
                    }
                    for domain, block in sorted(manifest["domains"].items())
                },
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
