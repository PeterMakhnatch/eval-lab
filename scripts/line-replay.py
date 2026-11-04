#!/usr/bin/env python3
"""Run the frozen, zero-spend standard line replay gate.

Usage: ``uv run python scripts/line-replay.py [--set PATH] [--name replay-v1]``.
The set defaults to EVALLAB_LINE_REPLAY_SET or ~/Developer/eval-lab-results/line/replay-v1.

The set's MANIFEST.json, spec.json and baseline.json are pinned by SHA-256 in the tracked
file src/evallab/line/replay_sets.json, and baseline-provenance.json must agree with the
pin, so changing the baseline or its inputs requires a reviewed PR that updates the pin.
Before replaying, every listed input is hashed, no unlisted or symlinked file may exist,
and every path in the spec must resolve inside the set's inputs/.

Exit: 0 pass, 1 regression, 2 invalid (broken set, tampering, bad replay input or a
summary that could not be produced), 77 SKIP only when the set directory is absent.
This tool never refreshes the baseline or writes Judge Station state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from evallab.line.replay import replay

REPO_ROOT = Path(__file__).resolve().parents[1]
PINS_PATH = REPO_ROOT / "src/evallab/line/replay_sets.json"
PINS_SCHEMA = "evallab.line_replay_pins/v1"
MANIFEST_SCHEMA = "evallab.line_replay_manifest/v1"
DEFAULT_SET = "~/Developer/eval-lab-results/line/replay-v1"
DEFAULT_NAME = "replay-v1"
# Not frozen inputs: the manifest, baseline files and README describe the set, while
# batches/ and baseline-history/ hold outputs and archives. Everything else is listed.
UNLISTED_FILES = {"MANIFEST.json", "baseline.json", "baseline-provenance.json", "README.md"}
UNLISTED_DIRS = {"batches", "baseline-history"}
VERDICTS = {0: "PASS", 1: "REGRESSION", 2: "INVALID"}


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _digest(path: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            hasher.update(chunk)
            size += len(chunk)
    return hasher.hexdigest(), size


def _count(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ValueError(f"{field} must be a SHA-256 hex digest")
    return value.lower()


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be nonempty text")
    return value


def _real(path: Path) -> Path:
    return Path(os.path.realpath(path))


def _inside(path: Path, base: Path) -> bool:
    return path == base or path.is_relative_to(base)


def _input_path(root: Path, value: Any) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("manifest path must be nonempty relative POSIX text")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or relative == Path("."):
        raise ValueError(f"manifest path escapes set: {value!r}")
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"manifest path escapes set: {value!r}")
    return path


def _load_pin(name: str) -> dict[str, str]:
    pins = _object(PINS_PATH)
    if pins.get("schema") != PINS_SCHEMA:
        raise ValueError(f"{PINS_PATH.name} has an unsupported schema")
    sets = pins.get("sets")
    entry = sets.get(name) if isinstance(sets, dict) else None
    if not isinstance(entry, dict):
        raise ValueError(f"no pinned replay set named {name!r} in {PINS_PATH.name}")
    pin = {
        field: _sha256(entry.get(field), f"pin {name} {field}")
        for field in ("manifest_sha256", "spec_sha256", "baseline_sha256")
    }
    pin["baseline_code_rev"] = _text(entry.get("baseline_code_rev"), f"pin {name} code rev")
    return pin


def _regular(root: Path, name: str) -> Path:
    path = root / name
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"replay set is missing a regular file: {name}")
    return path


def _check_pins(root: Path, pin: dict[str, str]) -> None:
    """The set's key files must be byte-identical to the reviewed pin."""
    for field, name in (
        ("manifest_sha256", "MANIFEST.json"),
        ("spec_sha256", "spec.json"),
        ("baseline_sha256", "baseline.json"),
    ):
        if _digest(_regular(root, name))[0] != pin[field]:
            raise ValueError(
                f"{name} does not match the pin in {PINS_PATH.name}; changing the frozen set "
                "or its baseline requires a reviewed PR that updates the pin"
            )
    provenance = _object(_regular(root, "baseline-provenance.json"))
    for field in ("manifest_sha256", "spec_sha256", "baseline_sha256"):
        if _sha256(provenance.get(field), f"baseline provenance {field}") != pin[field]:
            raise ValueError(f"baseline provenance {field} disagrees with the pin")
    if provenance.get("code_rev") != pin["baseline_code_rev"]:
        raise ValueError("baseline provenance code_rev disagrees with the pin")
    if provenance.get("code_dirty") is not False:
        raise ValueError("baseline provenance must record a clean code checkout")
    _text(provenance.get("authorized_by"), "baseline provenance authorized_by")
    _text(provenance.get("reason"), "baseline provenance reason")


def _check_files(root: Path) -> None:
    manifest = _object(root / "MANIFEST.json")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError("unsupported manifest schema")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("manifest files must be a nonempty list")
    if _count(manifest.get("input_files"), "manifest input_files") != len(files):
        raise ValueError("manifest input_files does not match files count")
    run_count = _count(manifest.get("run_count"), "manifest run_count")
    corpora = manifest.get("corpora")
    if not isinstance(corpora, dict) or any(
        not isinstance(name, str) or not name for name in corpora
    ):
        raise ValueError("manifest corpora must map corpus names to run counts")
    if sum(_count(n, f"manifest corpora.{name}") for name, n in corpora.items()) != run_count:
        raise ValueError("manifest corpus counts do not match run_count")
    seen: set[Path] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("manifest file entries must be objects")
        path = _input_path(root, entry.get("path"))
        if path in seen:
            raise ValueError(f"duplicate manifest input: {entry['path']}")
        seen.add(path)
        expected_hash = _sha256(entry.get("sha256"), f"manifest {entry['path']} sha256")
        expected_size = _count(entry.get("size"), f"manifest {entry['path']} size")
        if not isinstance(entry.get("kind"), str) or not entry["kind"]:
            raise ValueError(f"manifest {entry['path']} needs a kind")
        if "source" in entry and not isinstance(entry["source"], str):
            raise ValueError(f"manifest {entry['path']} source must be text")
        if not path.is_file():
            raise ValueError(f"manifest input missing or not a file: {entry['path']}")
        actual_hash, actual_size = _digest(path)
        if actual_size != expected_size or actual_hash != expected_hash:
            raise ValueError(f"manifest input hash/size mismatch: {entry['path']}")
    if root / "spec.json" not in seen:
        raise ValueError("manifest does not include spec.json")
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        for name in dirnames:
            if (here / name).is_symlink():
                raise ValueError(f"symlink in replay set: {(here / name).relative_to(root)}")
        if here == root:
            dirnames[:] = [name for name in dirnames if name not in UNLISTED_DIRS]
        for name in filenames:
            path = here / name
            relative = path.relative_to(root)
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"symlink or non-regular file in replay set: {relative}")
            if path in seen or (here == root and name in UNLISTED_FILES):
                continue
            raise ValueError(f"unlisted file in replay set: {relative.as_posix()}")


def _check_spec_paths(root: Path) -> None:
    """Everything the spec reads must be a listed file under inputs/, compared canonically."""
    spec = _object(root / "spec.json")
    real_root = _real(root)
    inputs, batches = real_root / "inputs", real_root / "batches"

    def where(value: Any, field: str) -> Path:
        path = Path(_text(value, f"spec {field}")).expanduser()
        return _real(path if path.is_absolute() else real_root / path)

    def frozen_input(value: Any, field: str) -> Path:
        path = where(value, field)
        if not _inside(path, inputs):
            raise ValueError(f"spec {field} resolves outside the set's inputs/: {value}")
        return path

    sources = spec.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("spec sources must be a nonempty list")
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("spec sources must be objects")
        rollouts = frozen_input(source.get("rollouts"), "rollouts")
        if source.get("labels") is not None:
            frozen_input(source["labels"], "labels")
        if rollouts.suffix == ".jsonl":
            for number, line in enumerate(rollouts.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError(f"{rollouts.name}:{number} must be an object")
                member = where(row.get("path"), f"{rollouts.name}:{number} path")
                if not _inside(member, inputs):
                    raise ValueError(
                        f"{rollouts.name}:{number} path resolves outside the set's inputs/"
                    )
    if spec.get("task_status") is not None:
        frozen_input(spec["task_status"], "task_status")
    autoreview = spec.get("autoreview")
    if isinstance(autoreview, dict) and autoreview.get("cache") is not None:
        frozen_input(autoreview["cache"], "autoreview.cache")
    judge = spec.get("judge")
    if not isinstance(judge, dict):
        raise ValueError("spec judge must be an object")
    # Without an explicit frozen verdicts file the line would read live Judge Station state.
    frozen_input(judge.get("verdicts"), "judge.verdicts")
    if not _inside(where(judge.get("queue"), "judge.queue"), real_root):
        raise ValueError("spec judge.queue resolves outside the set")
    if not _inside(where(spec.get("out_root"), "out_root"), batches):
        raise ValueError("spec out_root must resolve inside the set's batches/")


def _validate_set(root: Path, name: str) -> dict[str, Any]:
    if not root.is_dir():
        raise ValueError(f"replay set path is not a directory: {root}")
    _check_pins(root, _load_pin(name))
    _check_files(root)
    _check_spec_paths(root)
    return _object(root / "baseline.json")


def _rate(group: dict[str, Any], metric: str, denominator: str) -> str:
    value = group["metrics"][metric]
    k = _count(value["k"], f"{metric}.k")
    n = _count(value["n"], f"{metric}.n")
    if n != _count(group[denominator], denominator) or k > n:
        raise ValueError(f"invalid {metric} denominator")
    return f"{k}/{n} ({100 * k / n:.1f}%)" if n else "0/0 (n/a)"


def _describe(regression: Any) -> str:
    if not isinstance(regression, dict):
        return str(regression)
    if regression.get("kind") in ("quality_check", "corpus_missing"):
        what = regression.get("check") or regression.get("corpus")
        return f"{regression['kind']} {what}: {regression.get('details')}"
    try:
        return (
            f"{regression['corpus']} {regression['metric']}: {regression['before']:.3f} -> "
            f"{regression['after']:.3f}, {regression['changed_count']} of "
            f"{regression['denominator']} runs, allowed {regression['tolerance']:.3f}"
        )
    except (KeyError, TypeError, ValueError):
        return json.dumps(regression, sort_keys=True)


def _summary(result: dict[str, Any], baseline: dict[str, Any], status: int) -> None:
    """Print the one-screen summary; raises when it cannot be completed."""
    print(f"Verdict: {VERDICTS[status]}")
    if result.get("variant") is not None:
        print(f"Variant: {json.dumps(result['variant'], sort_keys=True)}")
    if result.get("error"):
        print(f"Replay error: {result['error']}", file=sys.stderr)
    regressions = result.get("regressions") or []
    for regression in regressions:
        print(f"  regression: {_describe(regression)}")
    for note in result.get("informational") or []:
        print(f"  note: {json.dumps(note, sort_keys=True)}")
    batch_value = result.get("batch")
    if not isinstance(batch_value, str) or not batch_value:
        raise ValueError("replay result has no batch path")
    batch = Path(batch_value)
    print(f"Batch: {batch}")
    changes = result.get("changed_runs")
    if changes is None:
        print(f"Changed decisions: unavailable ({result.get('changed_runs_unavailable')})")
    elif isinstance(changes, list) and all(isinstance(row, dict) for row in changes):
        print(f"Changed decisions: {len(changes)}")
        if changes:
            report = batch / "changed-decisions.json"
            report.write_text(
                json.dumps(changes, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            print(f"Changed decision report: {report}")
    else:
        raise ValueError("replay changed_runs must be a list of objects or null")
    previous = baseline["scoreboard"]["by_corpus"]
    current = _object(batch / "scoreboard.json")["by_corpus"]
    if not isinstance(previous, dict) or not isinstance(current, dict):
        raise ValueError("scoreboards need by_corpus objects")
    for corpus in sorted(previous.keys() | current.keys()):
        if corpus not in current:
            print(f"{corpus}: MISSING from this run (baseline has it)")
            continue
        if corpus not in previous:
            print(f"{corpus}: NEW in this run (not in the baseline)")
            continue
        recall = " -> ".join(
            _rate(group, "drop_or_review_recall", "cheats")
            for group in (previous[corpus], current[corpus])
        )
        alarms = " -> ".join(
            _rate(group, "clean_false_alarm_rate", "clean")
            for group in (previous[corpus], current[corpus])
        )
        print(f"{corpus}: recall {recall}; false alarms {alarms}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", dest="replay_set", type=Path, help="frozen replay set directory")
    parser.add_argument("--name", default=DEFAULT_NAME, help="pinned set name in replay_sets.json")
    parser.add_argument("--j3-cache", type=Path, default=os.environ.get("J3_CACHE") or None)
    parser.add_argument(
        "--goodfire-review-floor", choices=("0", "1"),
        default=os.environ.get("GOODFIRE_REVIEW_FLOOR") or None,
    )
    args = parser.parse_args(argv)
    root = (
        args.replay_set
        if args.replay_set is not None
        else Path(os.environ.get("EVALLAB_LINE_REPLAY_SET", DEFAULT_SET))
    ).expanduser()
    if not root.exists() and not root.is_symlink():
        print(
            f"SKIP: standard line replay set not found at {root} "
            "(set EVALLAB_LINE_REPLAY_SET or --set); nothing was checked",
            file=sys.stderr,
        )
        return 77
    try:
        root = root.resolve()
        baseline = _validate_set(root, args.name)
        variant: dict[str, Any] = {}
        if args.j3_cache is not None:
            variant["j3_cache"] = str(Path(args.j3_cache).expanduser().absolute())
        if args.goodfire_review_floor is not None:
            variant["goodfire_review_floor"] = args.goodfire_review_floor == "1"
        result, status = replay(
            root / "spec.json", root / "baseline.json", REPO_ROOT,
            **({"variant": variant} if variant else {}),
        )
        if isinstance(status, bool) or not isinstance(status, int) or status not in (0, 1, 2):
            raise ValueError("replay returned an invalid exit status")
        if not isinstance(result, dict):
            raise ValueError("replay result must be an object")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"Invalid standard line replay data: {exc}", file=sys.stderr)
        return 2
    try:
        _summary(result, baseline, status)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"Replay summary unavailable: {exc!r}", file=sys.stderr)
        return status or 2
    return status


if __name__ == "__main__":
    raise SystemExit(main())
