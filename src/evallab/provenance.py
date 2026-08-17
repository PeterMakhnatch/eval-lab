"""PROVENANCE: explicit task origin classification across corpora.

Deterministic local analysis only. Reuses craft discovery.
Never guesses; absent evidence yields origin=unknown with reason.
"""

from __future__ import annotations

import argparse
import os
import tomllib
from collections import defaultdict
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
    HARBOR_DERIVED = "harbor-derived"
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


def _extract_task_ref(task_dir: Path, source_root: Path) -> str:
    """Return declared name from task.toml or fallback to relative path."""
    manifest_path = task_dir / "task.toml"
    try:
        manifest = tomllib.loads(manifest_path.read_bytes().decode("utf-8"))
        declared = manifest.get("task", {}).get("name")
        if isinstance(declared, str) and declared:
            return declared
    except Exception:
        pass
    return task_dir.relative_to(source_root).as_posix()


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
    """Classify a single task_ref by scanning known roots.
    Detects multi-corpus collisions as harbor-derived.
    """
    repo_root = repository_root()
    tb3_path = tb3_root(tb3_explicit, environ)

    matches: list[tuple[Origin, Path, Path, str | None, str]] = []

    # check TB3
    if tb3_path.is_dir():
        for task_dir in discover_tasks(tb3_path):
            if _extract_task_ref(task_dir, tb3_path) == task_ref:
                matches.append(
                    (
                        Origin.HARBOR_NATIVE,
                        tb3_path,
                        task_dir,
                        "terminal-bench-3",
                        "tb3_root",
                    )
                )
                break

    # check local-lab
    lib_root = (repo_root / "library/tasks").resolve()
    if lib_root.is_dir():
        for task_dir in discover_tasks(lib_root):
            if _extract_task_ref(task_dir, lib_root) == task_ref:
                matches.append(
                    (Origin.LOCAL_LAB, lib_root, task_dir, None, "library/tasks")
                )
                break

    # check proposed
    prop_root = _proposed_root(repo_root)
    if prop_root.is_dir():
        for task_dir in discover_tasks(prop_root):
            if _extract_task_ref(task_dir, prop_root) == task_ref:
                matches.append(
                    (
                        Origin.PROPOSED,
                        prop_root,
                        task_dir,
                        None,
                        "library/tasks/_proposed",
                    )
                )
                break

    if not matches:
        return TaskOrigin(
            task_ref=task_ref,
            origin=Origin.UNKNOWN,
            family=None,
            corpus_root="unavailable",
            evidence=f"no matching task_ref in any known corpus root for {task_ref}",
            confidence=Confidence.UNKNOWN,
        )

    if len(matches) > 1:
        harbor_matches = [m for m in matches if m[0] == Origin.HARBOR_NATIVE]
        if harbor_matches:
            h = harbor_matches[0]
            path_strs = [m[2].as_posix() for m in matches]
            evidence = "multi-corpus resolution: " + "; ".join(path_strs)
            # strengthen inference by comparing instruction.md to upstream
            try:
                h_dir = h[2]
                h_inst_p = h_dir / "instruction.md"
                if h_inst_p.exists():
                    h_bytes = h_inst_p.read_bytes()
                    for m in matches:
                        if m[0] != Origin.HARBOR_NATIVE:
                            o_dir = m[2]
                            o_inst_p = o_dir / "instruction.md"
                            if o_inst_p.exists():
                                o_bytes = o_inst_p.read_bytes()
                                status = "identical" if o_bytes == h_bytes else "divergent"
                                evidence += (
                                    f"; instruction.md {status} ({o_dir.as_posix()})"
                                )
            except Exception:
                pass
            base = _classify_from_root(h[2], h[1], Origin.HARBOR_NATIVE, h[3], h[4])
            return TaskOrigin(
                task_ref=base.task_ref,
                origin=Origin.HARBOR_DERIVED,
                family=base.family,
                corpus_root=base.corpus_root,
                evidence=evidence,
                confidence=Confidence.INFERRED,
            )
        # non-harbor collision, take first
        m = matches[0]
        return _classify_from_root(m[2], m[1], m[0], m[3], m[4])

    # single unambiguous
    m = matches[0]
    return _classify_from_root(m[2], m[1], m[0], m[3], m[4])


def discover_all(
    *,
    tb3_explicit: Path | None = None,
    environ: dict[str, str] | None = None,
) -> list[TaskOrigin]:
    """Discover and classify every task from existing roots.
    Collisions resolved to harbor-derived. Absent root -> no tasks from it.
    """
    repo_root = repository_root()
    tb3_path = tb3_root(tb3_explicit, environ)

    seen: dict[str, list[tuple[Origin, Path, Path, str | None, str]]] = defaultdict(
        list
    )

    # TB3
    if tb3_path.is_dir():
        for task_dir in discover_tasks(tb3_path):
            eff = _extract_task_ref(task_dir, tb3_path)
            seen[eff].append(
                (Origin.HARBOR_NATIVE, tb3_path, task_dir, "terminal-bench-3", "tb3_root")
            )

    # local-lab
    lib_root = (repo_root / "library/tasks").resolve()
    if lib_root.is_dir():
        for task_dir in discover_tasks(lib_root):
            eff = _extract_task_ref(task_dir, lib_root)
            seen[eff].append(
                (Origin.LOCAL_LAB, lib_root, task_dir, None, "library/tasks")
            )

    # proposed
    prop_root = _proposed_root(repo_root)
    if prop_root.is_dir():
        for task_dir in discover_tasks(prop_root):
            eff = _extract_task_ref(task_dir, prop_root)
            seen[eff].append(
                (Origin.PROPOSED, prop_root, task_dir, None, "library/tasks/_proposed")
            )

    records: list[TaskOrigin] = []
    for _eff_name, locs in seen.items():
        if len(locs) > 1 and any(loc[0] == Origin.HARBOR_NATIVE for loc in locs):
            h_locs = [loc for loc in locs if loc[0] == Origin.HARBOR_NATIVE]
            h = h_locs[0]
            path_strs = [loc[2].as_posix() for loc in locs]
            evidence = "multi-corpus resolution: " + "; ".join(path_strs)
            try:
                h_dir = h[2]
                h_inst_p = h_dir / "instruction.md"
                if h_inst_p.exists():
                    h_bytes = h_inst_p.read_bytes()
                    for loc in locs:
                        if loc[0] != Origin.HARBOR_NATIVE:
                            o_dir = loc[2]
                            o_inst_p = o_dir / "instruction.md"
                            if o_inst_p.exists():
                                o_bytes = o_inst_p.read_bytes()
                                status = "identical" if o_bytes == h_bytes else "divergent"
                                evidence += (
                                    f"; instruction.md {status} ({o_dir.as_posix()})"
                                )
            except Exception:
                pass
            base = _classify_from_root(h[2], h[1], Origin.HARBOR_NATIVE, h[3], h[4])
            rec = TaskOrigin(
                task_ref=base.task_ref,
                origin=Origin.HARBOR_DERIVED,
                family=base.family,
                corpus_root=base.corpus_root,
                evidence=evidence,
                confidence=Confidence.INFERRED,
            )
            records.append(rec)
        else:
            for loc in locs:
                o, r, d, f, p = loc
                rec = _classify_from_root(d, r, o, f, p)
                records.append(rec)

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
        # always report status of every configured corpus root
        tb3_path = tb3_root(args.tb3_root, environ)
        repo_root = repository_root()
        lib_root = (repo_root / "library/tasks").resolve()
        prop_root = _proposed_root(repo_root)

        status_lines: list[str] = []
        root_header = "corpus\tstatus\tpath\ttask_count\treason"
        status_lines.append(root_header)

        # tb3
        if tb3_path.is_dir():
            try:
                tb3_count = sum(1 for _ in discover_tasks(tb3_path))
            except Exception:
                tb3_count = 0
            status_lines.append(
                f"tb3_root\tfound\t{tb3_path.as_posix()}\t{tb3_count}\t"
            )
        else:
            reason = "path does not exist" if not tb3_path.exists() else "not a directory"
            status_lines.append(
                f"tb3_root\tunavailable\t{tb3_path.as_posix()}\t0\t{reason}"
            )

        # local-lab
        if lib_root.is_dir():
            try:
                lib_count = sum(1 for _ in discover_tasks(lib_root))
            except Exception:
                lib_count = 0
            status_lines.append(
                f"local-lab\tfound\t{lib_root.as_posix()}\t{lib_count}\t"
            )
        else:
            reason = "path does not exist" if not lib_root.exists() else "not a directory"
            status_lines.append(
                f"local-lab\tunavailable\t{lib_root.as_posix()}\t0\t{reason}"
            )

        # proposed
        if prop_root.is_dir():
            try:
                prop_count = sum(1 for _ in discover_tasks(prop_root))
            except Exception:
                prop_count = 0
            status_lines.append(
                f"proposed\tfound\t{prop_root.as_posix()}\t{prop_count}\t"
            )
        else:
            reason = "path does not exist" if not prop_root.exists() else "not a directory"
            status_lines.append(
                f"proposed\tunavailable\t{prop_root.as_posix()}\t0\t{reason}"
            )

        print("\n".join(status_lines) + "\n")

        recs = discover_all(tb3_explicit=args.tb3_root, environ=environ)
        print(render_report(recs), end="")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
