"""PROVENANCE: explicit task origin classification across corpora.

Deterministic local analysis only. Reuses craft discovery.
Never guesses; absent evidence yields origin=unknown with reason.
"""

from __future__ import annotations

import argparse
import os
import tomllib
from enum import StrEnum
from pathlib import Path

from evallab.craft import (
    discover_tasks,
    repository_root,
    tb3_root,
)
from evallab.schemas import ContractModel


class Origin(StrEnum):
    """Task origin taxonomy."""

    HARBOR_NATIVE = "harbor-native"
    LOCAL_LAB = "local-lab"
    PROPOSED = "proposed"
    UNKNOWN = "unknown"


class Confidence(StrEnum):
    """Classification confidence."""

    CERTAIN = "certain"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class TaskOrigin(ContractModel):
    """Per-task provenance record."""

    task_ref: str
    origin: Origin
    family: str | None
    corpus_root: str
    evidence: str
    confidence: Confidence


def _proposed_root(repo_root: Path) -> Path:
    return (repo_root / "library/tasks/_proposed").resolve()


def _classify_from_root(
    task_dir: Path, source_root: Path, origin: Origin, family: str | None, evidence_prefix: str
) -> TaskOrigin:
    """Classify one discovered task dir."""
    manifest_path = task_dir / "task.toml"
    task_ref = task_dir.relative_to(source_root).as_posix()
    try:
        manifest = tomllib.loads(manifest_path.read_bytes().decode("utf-8"))
        task_table = manifest.get("task", {})
        declared = task_table.get("name")
        if isinstance(declared, str) and declared:
            task_ref = declared
    except Exception:
        pass  # fall back to relative path

    corpus_root = source_root.as_posix()
    evidence = f"{evidence_prefix}: {task_dir.as_posix()}"
    confidence = Confidence.CERTAIN if origin != Origin.UNKNOWN else Confidence.UNKNOWN

    return TaskOrigin(
        task_ref=task_ref,
        origin=origin,
        family=family,
        corpus_root=corpus_root,
        evidence=evidence,
        confidence=confidence,
    )


def classify_task(
    task_ref: str,
    *,
    tb3_explicit: Path | None = None,
    environ: dict[str, str] | None = None,
) -> TaskOrigin:
    """Classify a single task_ref by scanning known roots. Returns unknown on no match."""
    repo_root = repository_root()

    # TB3 harbor-native
    tb3_path = tb3_root(tb3_explicit, environ)
    if tb3_path.is_dir():
        for task_dir in discover_tasks(tb3_path):
            manifest = task_dir / "task.toml"
            try:
                doc = tomllib.loads(manifest.read_bytes().decode("utf-8"))
                name = doc.get("task", {}).get("name")
                rel = task_dir.relative_to(tb3_path).as_posix()
                if (isinstance(name, str) and name == task_ref) or rel == task_ref:
                    return _classify_from_root(
                        task_dir, tb3_path, Origin.HARBOR_NATIVE, "terminal-bench-3", "tb3_root"
                    )
            except Exception:
                rel = task_dir.relative_to(tb3_path).as_posix()
                if rel == task_ref:
                    return _classify_from_root(
                        task_dir, tb3_path, Origin.HARBOR_NATIVE, "terminal-bench-3", "tb3_root"
                    )

    # local-lab
    lib_root = (repo_root / "library/tasks").resolve()
    if lib_root.is_dir():
        for task_dir in discover_tasks(lib_root):
            manifest = task_dir / "task.toml"
            try:
                doc = tomllib.loads(manifest.read_bytes().decode("utf-8"))
                name = doc.get("task", {}).get("name")
                rel = task_dir.relative_to(lib_root).as_posix()
                if (isinstance(name, str) and name == task_ref) or rel == task_ref:
                    return _classify_from_root(
                        task_dir, lib_root, Origin.LOCAL_LAB, None, "library/tasks"
                    )
            except Exception:
                rel = task_dir.relative_to(lib_root).as_posix()
                if rel == task_ref:
                    return _classify_from_root(
                        task_dir, lib_root, Origin.LOCAL_LAB, None, "library/tasks"
                    )

    # proposed
    prop_root = _proposed_root(repo_root)
    if prop_root.is_dir():
        for task_dir in discover_tasks(prop_root):
            manifest = task_dir / "task.toml"
            try:
                doc = tomllib.loads(manifest.read_bytes().decode("utf-8"))
                name = doc.get("task", {}).get("name")
                rel = task_dir.relative_to(prop_root).as_posix()
                if (isinstance(name, str) and name == task_ref) or rel == task_ref:
                    return _classify_from_root(
                        task_dir, prop_root, Origin.PROPOSED, None, "library/tasks/_proposed"
                    )
            except Exception:
                rel = task_dir.relative_to(prop_root).as_posix()
                if rel == task_ref:
                    return _classify_from_root(
                        task_dir, prop_root, Origin.PROPOSED, None, "library/tasks/_proposed"
                    )

    # unknown
    return TaskOrigin(
        task_ref=task_ref,
        origin=Origin.UNKNOWN,
        family=None,
        corpus_root="unavailable",
        evidence=f"no matching task_ref in any known corpus root for {task_ref}",
        confidence=Confidence.UNKNOWN,
    )


def discover_all(
    *,
    tb3_explicit: Path | None = None,
    environ: dict[str, str] | None = None,
) -> list[TaskOrigin]:
    """Discover and classify every task from existing roots. Absent root -> no tasks from it."""
    repo_root = repository_root()
    records: list[TaskOrigin] = []

    # harbor-native TB3
    tb3_path = tb3_root(tb3_explicit, environ)
    if tb3_path.is_dir():
        for task_dir in discover_tasks(tb3_path):
            rec = _classify_from_root(
                task_dir, tb3_path, Origin.HARBOR_NATIVE, "terminal-bench-3", "tb3_root"
            )
            records.append(rec)

    # local-lab
    lib_root = (repo_root / "library/tasks").resolve()
    if lib_root.is_dir():
        for task_dir in discover_tasks(lib_root):
            rec = _classify_from_root(
                task_dir, lib_root, Origin.LOCAL_LAB, None, "library/tasks"
            )
            records.append(rec)

    # proposed
    prop_root = _proposed_root(repo_root)
    if prop_root.is_dir():
        for task_dir in discover_tasks(prop_root):
            rec = _classify_from_root(
                task_dir, prop_root, Origin.PROPOSED, None, "library/tasks/_proposed"
            )
            records.append(rec)

    # sort deterministic: by origin, then task_ref
    records.sort(key=lambda r: (r.origin.value, r.task_ref))
    return records


def render_report(records: list[TaskOrigin]) -> str:
    """Deterministic table, byte-identical, no timestamps."""
    header = "task_ref\torigin\tfamily\tcorpus_root\tconfidence\tevidence"
    if not records:
        return header + "\n"
    lines = [header]
    for r in records:
        fam = r.family or ""
        lines.append(
            f"{r.task_ref}\t{r.origin.value}\t{fam}\t{r.corpus_root}\t{r.confidence.value}\t{r.evidence}"
        )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evallab.provenance")
    sub = parser.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("classify", help="classify one task_ref")
    c.add_argument("task_ref", help="task name or relative path")
    c.add_argument("--tb3-root", type=Path, default=None, help="override TB3 root")

    r = sub.add_parser("report", help="full deterministic report of discovered tasks")
    r.add_argument("--tb3-root", type=Path, default=None, help="override TB3 root")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    environ = os.environ

    if args.cmd == "classify":
        rec = classify_task(args.task_ref, tb3_explicit=args.tb3_root, environ=environ)
        print(f"task_ref={rec.task_ref}")
        print(f"origin={rec.origin.value}")
        print(f"family={rec.family or 'null'}")
        print(f"confidence={rec.confidence.value}")
        print(f"corpus_root={rec.corpus_root}")
        print(f"evidence={rec.evidence}")
        return 0

    if args.cmd == "report":
        recs = discover_all(tb3_explicit=args.tb3_root, environ=environ)
        print(render_report(recs), end="")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
