#!/usr/bin/env python3
"""Compare the full DEV line on committed revisions, without refreshing a baseline."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SET = "~/Developer/eval-lab-results/line/replay-v1"
DECISIONS = ("keep", "review", "drop")


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()


def line_paths(root: Path, repository_files: set[str] | None = None) -> list[str]:
    """Conservative transitive local-import closure, including conditional imports."""
    pending = [root / "scripts/line-replay.py"]
    seen: set[Path] = set()

    def module(name: str) -> None:
        if not name.startswith("evallab"):
            return
        parts = name.split(".")
        for n in range(1, len(parts) + 1):
            path = root / "src" / Path(*parts[:n])
            for candidate in (path.with_suffix(".py"), path / "__init__.py"):
                if candidate.is_file() and candidate not in seen:
                    pending.append(candidate)

    resources: set[str] = set()
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        package = (
            ".".join(path.relative_to(root / "src").parts[:-1])
            if path.is_relative_to(root / "src")
            else ""
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module(alias.name)
            elif isinstance(node, ast.ImportFrom):
                prefix = node.module or ""
                if node.level:
                    prefix = ".".join(
                        package.split(".")[: len(package.split(".")) - node.level + 1]
                        + ([prefix] if prefix else [])
                    )
                module(prefix)
                for alias in node.names:
                    module(f"{prefix}.{alias.name}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                # Repository resource literals, not external frozen input paths.
                value = node.value
                if "\n" not in value and len(value) < 240 and not value.startswith("/"):
                    for candidate in (root / value, path.parent / value):
                        if candidate.is_relative_to(root):
                            relative = candidate.relative_to(root).as_posix()
                            if candidate.is_file() or (
                                repository_files is not None and relative in repository_files
                            ):
                                resources.add(relative)
    # Environment changes can alter imported behavior. The replay pin and gate
    # controls are also gate-sensitive, independently of decision-code imports.
    resources.update(
        (
            "pyproject.toml",
            "uv.lock",
            "src/evallab/line/replay_sets.json",
            "scripts/line-gate.py",
            "policy/standing-approvals.yaml",
        )
    )
    return sorted(resources | {p.relative_to(root).as_posix() for p in seen})


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
    policy = yaml.safe_load(policy_text)
    if not isinstance(policy, dict) or not isinstance(policy.get("line_gate_approvals", []), list):
        raise ValueError("invalid standing policy approval records")
    deltas = [
        {"corpus": row["corpus"], "class": row["class"], "delta": row["delta"]}
        for row in rows
        if any(row["delta"].values())
    ]
    for entry in policy.get("line_gate_approvals", []):
        if (
            isinstance(entry, dict)
            and pr is not None
            and entry.get("pr") == pr
            and entry.get("head") == head
            and entry.get("approved_by") == "Peter"
            and isinstance(entry.get("approval_quote"), str)
            and entry["approval_quote"].strip()
            and entry.get("corpus_deltas") == deltas
        ):
            return entry
    return None


RUNNER = """
import json, sys, tempfile
from pathlib import Path
from uuid import uuid4
root, data, output = map(Path, sys.argv[1:])
sys.path.insert(0, str(root / 'src'))
from evallab.line.spec import load_spec
from evallab.line.core import run_line, check_batch
spec = load_spec(data / 'spec.json')
ar = spec.get('autoreview', {})
if ar.get('mode') not in ('off', 'cached') or ar.get('max_usd') != 0:
    raise ValueError('line gate requires off/cached AutoReview and zero spend')
spec['batch_id'] += '-gate-' + uuid4().hex
spec['judge']['write'] = False
with tempfile.TemporaryDirectory(prefix='evallab-line-gate-spec-') as scratch:
    path = Path(scratch) / 'spec.json'
    path.write_text(json.dumps(spec))
    batch = run_line(path, root)
checks = check_batch(batch, root)
output.write_text(json.dumps({'batch': str(batch), 'checks': checks}, indent=2) + '\\n')
if not checks.get('passed'):
    raise SystemExit(2)
"""


def run_revision(root: Path, revision: str, data: Path, output: Path, label: str) -> dict[str, Any]:
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
    parser.add_argument("--head", default="HEAD")
    parser.add_argument(
        "--against", default="origin/main", help="merge-base reference; default origin/main"
    )
    parser.add_argument(
        "--set", type=Path, default=Path(os.environ.get("EVALLAB_LINE_REPLAY_SET", DEFAULT_SET))
    )
    parser.add_argument(
        "--output", type=Path, help="new receipt directory; defaults to a unique /tmp directory"
    )
    parser.add_argument("--pr", type=int)
    parser.add_argument(
        "--paths",
        action="store_true",
        help="list derived paths and changed paths only; not a gate pass",
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
        base = git(ROOT, "merge-base", args.against, head)
        report.update(head=head, base=base, origin_main=main_rev, pr=args.pr)
        # Resolve imports at both revisions, so removed and newly imported modules
        # are both sensitive. git archive avoids inspecting a dirty caller tree.
        paths: set[str] = set()
        for rev in (base, head):
            with tempfile.TemporaryDirectory(prefix="evallab-line-paths-") as scratch:
                tree = Path(scratch)
                archive = subprocess.run(
                    [
                        "git",
                        "archive",
                        rev,
                        "src/",
                        "scripts/line-replay.py",
                        "pyproject.toml",
                        "uv.lock",
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    check=True,
                ).stdout
                subprocess.run(["tar", "-xf", "-", "-C", str(tree)], input=archive, check=True)
                repository_files = set(git(ROOT, "ls-tree", "-r", "--name-only", rev).splitlines())
                paths.update(line_paths(tree, repository_files))
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
            before = run_revision(ROOT, base, data, output, "base")
            after = run_revision(ROOT, head, data, output, "head")
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
