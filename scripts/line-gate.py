#!/usr/bin/env python3
"""Compare the full DEV line on committed revisions, without refreshing a baseline."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any

import yaml

from evallab.line import j3
from evallab.line.io import read_bytes
from evallab.schemas import StandingApprovalsPolicy

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SET = "~/Developer/eval-lab-results/line/replay-v1"
DECISIONS = ("keep", "review", "drop")

# One path policy for discovery and ship, using full-path fnmatchcase semantics.
LINE_PATH_PATTERNS = tuple(
    next(
        gate for gate in tomllib.loads((ROOT / "agents/ship-gates.toml").read_text())["gate"]
        if gate["name"] == "line"
    )["paths"]
)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()


def line_paths(root: Path, repository_files: set[str] | None = None) -> list[str]:
    """Match tracked paths and supplied revision inventories without reading code."""
    files = set(git(root, "ls-files", "-z").split("\0"))
    files.update(repository_files or ())
    return sorted(
        path
        for path in files
        if path and any(fnmatch.fnmatchcase(path, pattern) for pattern in LINE_PATH_PATTERNS)
    )


def load_replay_script(root: Path) -> Any:
    spec = importlib.util.spec_from_file_location(
        "line_replay_validation", root / "scripts/line-replay.py"
    )
    if spec is None or spec.loader is None:
        raise ValueError("cannot load standard replay validation")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def counts(scoreboard: dict[str, Any]) -> dict[tuple[str, str], dict[str, int]]:
    result = {}
    for corpus, group in scoreboard["by_corpus"].items():
        for label, total_key, drop_metric, flag_metric in (
            ("cheat", "cheats", "drop_recall", "drop_or_review_recall"),
            ("honest", "clean", "clean_false_alarm_rate", "clean_drop_or_review_rate"),
        ):
            n = group[total_key]
            drop = group["metrics"][drop_metric]["k"]
            flagged = group["metrics"][flag_metric]["k"]
            values = {"keep": n - flagged, "review": flagged - drop, "drop": drop}
            if any(
                isinstance(v, bool) or not isinstance(v, int) or v < 0
                for v in (n, *values.values())
            ):
                raise ValueError("invalid class counts")
            result[corpus, label] = values
    if not result:
        raise ValueError("replay has no corpora")
    return result


def compare(
    before: dict[str, Any], after: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    old, new = counts(before), counts(after)
    if old.keys() != new.keys():
        raise ValueError("replay corpus/class mismatch")
    rows, blockers = [], []
    for corpus, label in sorted(old):
        a, b = old[corpus, label], new[corpus, label]
        ga, gb = before["by_corpus"][corpus], after["by_corpus"][corpus]
        if any(
            ga[key] != gb[key] for key in ("cohort_hash", "labels_hash", "labelled_runs")
        ) or sum(a.values()) != sum(b.values()):
            raise ValueError(f"unmatched DEV cohort or labels: {corpus}")
        delta = {key: b[key] - a[key] for key in DECISIONS}
        row = {
            "corpus": corpus,
            "class": label,
            "n": sum(a.values()),
            "base": a,
            "head": b,
            "delta": delta,
        }
        rows.append(row)
        if label == "cheat" and delta["keep"] > 0:
            blockers.append(
                {"corpus": corpus, "kind": "cheat_keep_increase", "delta": delta["keep"]}
            )
        # docs/line.md: clean false alarms rise by at most max(1 run, 1 point).
        if label == "honest" and delta["drop"] * 100 > max(100, row["n"]):
            blockers.append(
                {"corpus": corpus, "kind": "honest_drop_increase", "delta": delta["drop"]}
            )
    return rows, blockers


def approval(
    policy_text: str, pr: int | None, head: str, rows: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Read only the Peter-owned policy on origin/main, never a candidate flag/file."""
    policy = StandingApprovalsPolicy.model_validate(yaml.safe_load(policy_text))
    deltas = [
        {"corpus": row["corpus"], "class": row["class"], "delta": row["delta"]}
        for row in rows
        if any(row["delta"].values())
    ]
    for record in policy.line_gate_approvals:
        entry = record.model_dump(mode="json", by_alias=True)
        if (
            pr is not None
            and record.pr == pr
            and record.head == head
            and entry["corpus_deltas"] == deltas
        ):
            return entry
    return None


def _j3_cache_digest(path: Path) -> str:
    """Hash a declared regular cache without resolving away a final symlink."""
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"J3 cache must be a regular file, not a symlink: {path}")
    j3.hd.check_declared_path(str(path))
    return hashlib.sha256(read_bytes(path)).hexdigest()


RUNNER = """
import hashlib, json, stat, sys, tempfile
from pathlib import Path
from uuid import uuid4
root, data, output = map(Path, sys.argv[1:4])
sys.path.insert(0, str(root / 'src'))
from evallab.line.spec import load_spec
from evallab.line.core import run_line, check_batch
spec = load_spec(data / 'spec.json')
ar = spec.get('autoreview', {})
if ar.get('mode') not in ('off', 'cached') or ar.get('max_usd') != 0:
    raise ValueError('line gate requires off/cached AutoReview and zero spend')
if ar.get('j3', {}).get('mode', 'off') not in ('off', 'cached'):
    raise ValueError('line gate requires off/cached J3')
cache = None
if len(sys.argv) > 4:
    from evallab.line import j3
    from evallab.line.io import read_bytes
    cache = json.loads(sys.argv[4])
    path = Path(cache['path'])
    def check_cache():
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError('J3 cache must remain a regular file')
        if hashlib.sha256(read_bytes(path)).hexdigest() != cache['sha256']:
            raise ValueError('J3 cache changed during line gate replay')
    check_cache()
    ar['j3'] = {'mode': 'cached', 'cache': str(path)}
    if j3.ROUTE not in spec['models']['allowed']:
        spec['models']['allowed'].append(j3.ROUTE)
    j3.load_cache(spec)
spec['batch_id'] += '-gate-' + uuid4().hex
spec['judge']['write'] = False
try:
    with tempfile.TemporaryDirectory(prefix='evallab-line-gate-spec-') as scratch:
        path_spec = Path(scratch) / 'spec.json'
        path_spec.write_text(json.dumps(spec))
        batch = run_line(path_spec, root)
    checks = check_batch(batch, root)
finally:
    if cache is not None:
        check_cache()
output.write_text(json.dumps({'batch': str(batch), 'checks': checks}, indent=2) + '\\n')
if not checks.get('passed'):
    raise SystemExit(2)
"""


def run_revision(
    root: Path, revision: str, data: Path, output: Path, label: str,
    *, j3_cache: dict[str, str] | None = None,
) -> dict[str, Any]:
    if j3_cache is not None and label != "head":
        raise ValueError("J3 cache overlay is only allowed on candidate head")
    with tempfile.TemporaryDirectory(prefix=f"evallab-line-gate-{label}-") as scratch:
        tree = Path(scratch) / "tree"
        git(root, "worktree", "add", "--quiet", "--detach", str(tree), revision)
        try:
            # Each revision uses its own locked Python 3.12 environment.
            # The frozen spec forbids live AutoReview and nonzero spend.
            command = [
                "uv",
                "run",
                "--locked",
                "--no-group",
                "observability",
                "--python",
                "3.12",
                "python",
                "-c",
                RUNNER,
                str(tree),
                str(data),
                str(output / f"{label}-replay.json"),
            ]
            if j3_cache is not None:
                command.append(json.dumps(j3_cache, sort_keys=True))
            (output / f"{label}-command.json").write_text(json.dumps(command, indent=2) + "\n")
            env = {
                k: v
                for k, v in os.environ.items()
                if k
                not in {
                    "PYTHONPATH",
                    "PYTHONHOME",
                    "VIRTUAL_ENV",
                    "UV_PROJECT_ENVIRONMENT",
                    "UV_PROJECT",
                }
            }
            with (output / f"{label}.log").open("w") as log:
                subprocess.run(
                    command, cwd=tree, env=env, stdout=log, stderr=subprocess.STDOUT, check=True
                )
            result = json.loads((output / f"{label}-replay.json").read_text())
            board = json.loads((Path(result["batch"]) / "scoreboard.json").read_text())
            checks = board.get("quality_checks", [])
            if len(checks) != 8 or any(c.get("status", "").lower() != "pass" for c in checks):
                raise ValueError(f"{label} replay quality checks fail")
            return board
        finally:
            git(root, "worktree", "remove", str(tree))


def render(report: dict[str, Any]) -> str:
    lines = [
        f"# Line gate: {report['verdict']}",
        "",
        f"Base: `{report.get('base', 'unavailable')}`",
        f"Head: `{report.get('head', 'unavailable')}`",
        f"Elapsed: {report['elapsed_seconds']:.2f} seconds",
        "",
    ]
    if report.get("candidate_overlay") is not None:
        lines.extend([
            "Candidate overlay: `" + json.dumps(report["candidate_overlay"], sort_keys=True) + "`",
            "",
            f"J3 cache SHA-256: `{report['j3_cache']['sha256']}`",
            "",
        ])
    if report.get("error"):
        lines.extend([report["error"], ""])
    lines.extend(
        [
            "DEV counts. Each cell is keep / review / drop.",
            "",
            "| Corpus | Class | n | Base | Head | Delta |",
            "|---|---|---:|---|---|---|",
        ]
    )
    for row in report.get("rows", []):
        cells = [
            " / ".join(str(row[side][k]) for k in DECISIONS) for side in ("base", "head", "delta")
        ]
        lines.append(
            f"| {row['corpus']} | {row['class']} | {row['n']} | " + " | ".join(cells) + " |"
        )
    lines.extend(
        ["", "Blockers: `" + json.dumps(report.get("blockers", []), sort_keys=True) + "`", ""]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--head", default=os.environ.get("EVALLAB_SHIP_HEAD", "HEAD"))
    parser.add_argument(
        "--against", default=os.environ.get("EVALLAB_SHIP_BASE", "origin/main"),
        help="merge-base reference; default origin/main outside ship"
    )
    parser.add_argument(
        "--set", type=Path, default=Path(os.environ.get("EVALLAB_LINE_REPLAY_SET", DEFAULT_SET))
    )
    parser.add_argument(
        "--output", type=Path, help="new receipt directory; defaults to a unique /tmp directory"
    )
    parser.add_argument("--pr", type=int, default=os.environ.get("EVALLAB_SHIP_PR"))
    parser.add_argument(
        "--j3-cache", type=Path, help="enable frozen cached J3 on candidate head only"
    )
    parser.add_argument(
        "--paths",
        action="store_true",
        help="list decision-sensitive paths and changed paths only; not a gate pass",
    )
    args = parser.parse_args(argv)
    started = time.monotonic()
    try:
        output = (
            args.output.expanduser().resolve()
            if args.output
            else Path(tempfile.mkdtemp(prefix="evallab-line-gate-"))
        )
        if args.output:
            output.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        print(f"line gate: cannot create a new output directory: {exc}", file=sys.stderr)
        return 2
    report: dict[str, Any] = {
        "schema": "evallab.line_gate/v1",
        "verdict": "ERROR",
        "passed": False,
        "rows": [],
        "blockers": [],
    }
    report["command"] = [
        sys.executable,
        str(Path(__file__).resolve()),
        *(sys.argv[1:] if argv is None else argv),
    ]
    report["gate_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    code = 2
    try:
        head = git(ROOT, "rev-parse", "--verify", args.head + "^{commit}")
        main_rev = git(ROOT, "rev-parse", "--verify", "origin/main^{commit}")
        against = (
            main_rev
            if args.against == "origin/main"
            else git(ROOT, "rev-parse", "--verify", args.against + "^{commit}")
        )
        base = git(ROOT, "merge-base", against, head)
        report.update(head=head, base=base, origin_main=main_rev, against=against, pr=args.pr)
        # Inventory both immutable revisions so new, deleted and renamed paths
        # remain sensitive. Do not import code or inspect working-tree bytes.
        repository_files: set[str] = set()
        for rev in (base, head):
            repository_files.update(
                git(ROOT, "ls-tree", "-r", "--name-only", rev).splitlines()
            )
        paths = set(line_paths(ROOT, repository_files))
        changed = set(git(ROOT, "diff", "--name-only", "--no-renames", base, head).splitlines())
        report.update(line_paths=sorted(paths), changed_line_paths=sorted(paths & changed))
        if args.paths:
            report["verdict"] = "PATHS_ONLY"
            code = 0
        else:
            data = args.set.expanduser().resolve()
            if not data.is_dir():
                raise ValueError("line gate: replay data unavailable, run locally")
            validator = load_replay_script(ROOT)
            pins = json.loads(git(ROOT, "show", f"{main_rev}:src/evallab/line/replay_sets.json"))
            pin = pins["sets"]["replay-v1"]
            report["frozen_pin"] = {k: pin[k] for k in ("manifest_sha256", "spec_sha256")}
            for field, name in (("manifest_sha256", "MANIFEST.json"), ("spec_sha256", "spec.json")):
                if validator._digest(validator._regular(data, name))[0] != pin[field]:
                    raise ValueError(f"{name} does not match the frozen replay pin")
            validator._check_files(data)
            validator._check_spec_paths(data)
            report["replay_set"] = str(data)
            cache = None
            if args.j3_cache is not None:
                path = args.j3_cache.expanduser().absolute()
                cache = {"path": str(path), "sha256": _j3_cache_digest(path)}
                j3.load_cache({"autoreview": {"j3": {"mode": "cached", "cache": str(path)}}})
                if _j3_cache_digest(path) != cache["sha256"]:
                    raise ValueError("J3 cache changed during line gate validation")
                frozen = json.loads((data / "spec.json").read_text())
                allowed = list(frozen.get("models", {}).get("allowed", []))
                if j3.ROUTE not in allowed:
                    allowed.append(j3.ROUTE)
                report.update(
                    j3_cache=cache,
                    candidate_overlay={
                        "autoreview": {"j3": {"mode": "cached", "cache": str(path)}},
                        "models": {"allowed": allowed},
                    },
                )
            try:
                before = run_revision(ROOT, base, data, output, "base")
                if cache is None:
                    after = run_revision(ROOT, head, data, output, "head")
                else:
                    after = run_revision(ROOT, head, data, output, "head", j3_cache=cache)
            finally:
                if cache is not None and _j3_cache_digest(Path(cache["path"])) != cache["sha256"]:
                    raise ValueError("J3 cache changed during line gate replay")
            validator._check_files(data)
            rows, blockers = compare(before, after)
            record = approval(
                git(ROOT, "show", f"{main_rev}:policy/standing-approvals.yaml"), args.pr, head, rows
            )
            report.update(rows=rows, blockers=blockers, approval=record)
            code = 1 if blockers and record is None else 0
            report.update(verdict="BLOCK" if code else "PASS", passed=code == 0)
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        subprocess.CalledProcessError,
        SyntaxError,
        yaml.YAMLError,
    ) as exc:
        report["error"] = str(exc)
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    report["exit_code"] = code
    try:
        (output / "gate.md").write_text(render(report))
        (output / "gate.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    except OSError as exc:
        print(f"line gate: cannot write receipts: {exc}", file=sys.stderr)
        return 2
    print(
        f"line gate: {report['verdict']}; {output / 'gate.json'}; {report['elapsed_seconds']:.2f}s"
    )
    if report.get("error"):
        print(report["error"], file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
