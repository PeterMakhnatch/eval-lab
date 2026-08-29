"""PROVENANCE: explicit task origin classification across corpora.

Deterministic local analysis only. Reuses craft discovery.
Never guesses; absent evidence yields origin=unknown with reason.
"""

from __future__ import annotations

import argparse
import os
import tomllib
from collections import defaultdict
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path

from evallab.craft import (
    discover_tasks,
    repository_root,
    tb3_root,
    tb4_root,
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


def _collect_locations(
    repo_root: Path,
    tb3_path: Path,
    tb4_path: Path,
) -> dict[str, list[tuple[Origin, Path, Path, str | None, str]]]:
    """Gather per-task_ref locations across TB3, TB4, local-lab, and proposed.

    A (family, root, dir, prefix) tuple per match; TB3 and TB4 both classify as
    HARBOR_NATIVE but carry distinct version families, so their records never
    collide on a shared task_ref.
    """
    seen: dict[str, list[tuple[Origin, Path, Path, str | None, str]]] = defaultdict(list)
    if tb3_path.is_dir():
        for task_dir in discover_tasks(tb3_path):
            ref = _extract_task_ref(task_dir, tb3_path)
            seen[ref].append(
                (Origin.HARBOR_NATIVE, tb3_path, task_dir, "terminal-bench-3", "tb3_root")
            )
    if tb4_path.is_dir():
        for task_dir in discover_tasks(tb4_path):
            ref = _extract_task_ref(task_dir, tb4_path)
            seen[ref].append(
                (Origin.HARBOR_NATIVE, tb4_path, task_dir, "terminal-bench-4", "tb4_root")
            )
    lib_root = (repo_root / "library/tasks").resolve()
    if lib_root.is_dir():
        for task_dir in discover_tasks(lib_root):
            ref = _extract_task_ref(task_dir, lib_root)
            seen[ref].append(
                (Origin.LOCAL_LAB, lib_root, task_dir, None, "library/tasks")
            )
    prop_root = _proposed_root(repo_root)
    if prop_root.is_dir():
        for task_dir in discover_tasks(prop_root):
            ref = _extract_task_ref(task_dir, prop_root)
            seen[ref].append(
                (Origin.PROPOSED, prop_root, task_dir, None, "library/tasks/_proposed")
            )
    return seen


def _resolve_locations(
    locs: list[tuple[Origin, Path, Path, str | None, str]],
) -> list[TaskOrigin]:
    """Turn one task_ref's locations into lane-aware TaskOrigin records.

    Harbor-native roots are grouped by version family (`terminal-bench-3` /
    `terminal-bench-4`) so the two lanes never collide with each other. A harbor
    lane that *also* appears in a non-harbor root (local-lab/proposed) is a
    duplicate of upstream -> HARBOR_DERIVED, exactly as before.
    """
    harbor = [loc for loc in locs if loc[0] == Origin.HARBOR_NATIVE]
    other = [loc for loc in locs if loc[0] != Origin.HARBOR_NATIVE]
    if not harbor:
        return [_classify_from_root(d, r, o, f, p) for o, r, d, f, p in locs]

    lanes: dict[str, list[tuple[Path, Path, str]]] = defaultdict(list)
    for _o, r, d, f, p in harbor:
        lanes[f or "harbor-native"].append((r, d, p))

    records: list[TaskOrigin] = []
    for family, entries in sorted(lanes.items()):
        r, d, p = entries[0]
        if other:
            path_strs = [e[1].as_posix() for e in entries] + [o[2].as_posix() for o in other]
            evidence = "multi-corpus resolution: " + "; ".join(path_strs)
            try:
                h_inst = d / "instruction.md"
                if h_inst.exists():
                    h_bytes = h_inst.read_bytes()
                    for o in other:
                        o_inst = o[2] / "instruction.md"
                        if o_inst.exists():
                            status = (
                                "identical" if o_inst.read_bytes() == h_bytes else "divergent"
                            )
                            evidence += f"; instruction.md {status} ({o_inst.as_posix()})"
            except Exception:
                pass
            base = _classify_from_root(d, r, Origin.HARBOR_NATIVE, family, p)
            records.append(
                TaskOrigin(
                    task_ref=base.task_ref,
                    origin=Origin.HARBOR_DERIVED,
                    family=family,
                    corpus_root=base.corpus_root,
                    evidence=evidence,
                    confidence=Confidence.INFERRED,
                )
            )
        else:
            records.append(_classify_from_root(d, r, Origin.HARBOR_NATIVE, family, p))
    return records


def classify_task(
    task_ref: str,
    *,
    tb3_explicit: Path | None = None,
    tb4_explicit: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> TaskOrigin:
    """Classify a single task_ref by scanning known roots.

    Lane-aware: a task present in both TB3 and TB4 roots resolves to the TB4
    lane (version-aware) rather than a spurious cross-version collision. A
    harbor lane that also exists in a local root stays HARBOR_DERIVED.
    """
    repo_root = repository_root()
    tb3_path = tb3_root(tb3_explicit, environ if isinstance(environ, dict) else None)
    tb4_path = tb4_root(tb4_explicit, environ if isinstance(environ, dict) else None)

    locs = _collect_locations(repo_root, tb3_path, tb4_path).get(task_ref, [])
    if not locs:
        return TaskOrigin(
            task_ref=task_ref,
            origin=Origin.UNKNOWN,
            family=None,
            corpus_root="unavailable",
            evidence=f"no matching task_ref in any known corpus root for {task_ref}",
            confidence=Confidence.UNKNOWN,
        )

    resolved = _resolve_locations(locs)
    if len(resolved) > 1:
        tb4 = [r for r in resolved if r.family == "terminal-bench-4"]
        if tb4:
            return tb4[0]
    return resolved[0]


def discover_all(
    *,
    tb3_explicit: Path | None = None,
    tb4_explicit: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> list[TaskOrigin]:
    """Discover and classify every task from existing roots.

    Lane-aware: TB3 and TB4 roots both classify as HARBOR_NATIVE with distinct
    version families, so a task shared across versions yields one record per
    lane and never a spurious cross-version collision. A harbor lane duplicated
    in local-lab/proposed resolves to HARBOR_DERIVED. Absent root -> no tasks
    from it.
    """
    repo_root = repository_root()
    tb3_path = tb3_root(tb3_explicit, environ if isinstance(environ, dict) else None)
    tb4_path = tb4_root(tb4_explicit, environ if isinstance(environ, dict) else None)

    seen = _collect_locations(repo_root, tb3_path, tb4_path)

    records: list[TaskOrigin] = []
    for _eff_name, locs in sorted(seen.items()):
        records.extend(_resolve_locations(locs))

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
    c.add_argument("--tb4-root", type=Path, default=None, help="override TB4 root")

    r = sub.add_parser("report", help="full deterministic report of discovered tasks")
    r.add_argument("--tb3-root", type=Path, default=None, help="override TB3 root")
    r.add_argument("--tb4-root", type=Path, default=None, help="override TB4 root")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    environ = os.environ

    if args.cmd == "classify":
        rec = classify_task(
            args.task_ref,
            tb3_explicit=args.tb3_root,
            tb4_explicit=args.tb4_root,
            environ=environ,
        )
        print(f"task_ref={rec.task_ref}")
        print(f"origin={rec.origin.value}")
        print(f"family={rec.family or 'null'}")
        print(f"confidence={rec.confidence.value}")
        print(f"corpus_root={rec.corpus_root}")
        print(f"evidence={rec.evidence}")
        return 0

    if args.cmd == "report":
        # always report status of every configured corpus root
        tb3_path = tb3_root(args.tb3_root)
        tb4_path = tb4_root(args.tb4_root)
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

        # tb4
        if tb4_path.is_dir():
            try:
                tb4_count = sum(1 for _ in discover_tasks(tb4_path))
            except Exception:
                tb4_count = 0
            status_lines.append(
                f"tb4_root\tfound\t{tb4_path.as_posix()}\t{tb4_count}\t"
            )
        else:
            reason = "path does not exist" if not tb4_path.exists() else "not a directory"
            status_lines.append(
                f"tb4_root\tunavailable\t{tb4_path.as_posix()}\t0\t{reason}"
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

        recs = discover_all(
            tb3_explicit=args.tb3_root, tb4_explicit=args.tb4_root, environ=environ
        )
        print(render_report(recs), end="")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
