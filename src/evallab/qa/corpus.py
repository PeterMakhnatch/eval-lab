"""Model-free corpus audit: one reference-solution run per task (``evallab qa corpus``).

Each task's own ``solution/solve.sh`` runs once through the native Harbor Modal
trial path (``ModalBackend.control``) under one run-wide cloud spend gate. No
agent, model, candidate or mutation stage runs. Rows are classified from the
observed reward and trial status only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shlex
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from evallab.dataset_audit_contracts import AuditObservation
from evallab.qa.task import _cap_excerpt, finite_reward
from evallab.regrade import SpendCapReached

CLASSES = ("clean", "reference_below_one", "cannot_run", "no_reference")
STUB_SHA256_PREFIX = "4e25c8e51ff8"
STUB_TEXT = "no solution written"
EXCERPT_LIMIT = 4_000
METHOD = (
    "Method: each task's own solution/solve.sh ran once in a fresh native Harbor Modal trial "
    "with its own verifier; no agent, model, candidate or mutation stage ran."
)

TrialRunner = Callable[[Path], AuditObservation]


def is_stub(data: bytes) -> bool:
    """True for the known placeholder bytes or any single command printing the stub text."""
    if hashlib.sha256(data).hexdigest().startswith(STUB_SHA256_PREFIX):
        return True
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    lines = [s for s in (line.strip() for line in text.splitlines()) if s and not s.startswith("#")]
    if len(lines) != 1:
        return False
    try:
        tokens = shlex.split(lines[0])
    except ValueError:
        return False
    if not tokens or tokens[0] not in {"echo", "printf"}:
        return False
    words = [token for token in tokens[1:] if not token.startswith("-")]
    return " ".join(words).removesuffix("\\n").strip() == STUB_TEXT


def classify(*, reward: float | None, status: str) -> str:
    """Class of a reference run that executed; ``no_reference`` is decided before running."""
    if status != "executed" or reward is None:
        return "cannot_run"
    return "clean" if reward == 1 else "reference_below_one"


def task_key(package: Path, corpus_root: Path) -> str:
    relative = package.resolve().relative_to(corpus_root.resolve()).as_posix()
    return package.resolve().name if relative == "." else relative


def select_tasks(
    packages: Sequence[Path],
    *,
    corpus_root: Path,
    only: Sequence[str] = (),
    done: frozenset[str] = frozenset(),
    limit: int | None = None,
) -> list[Path]:
    """Filter by ``--task`` (relative path or directory name), drop audited tasks, then limit."""
    wanted = set(only)
    selected = [
        package
        for package in packages
        if (not wanted or task_key(package, corpus_root) in wanted or package.name in wanted)
        and task_key(package, corpus_root) not in done
    ]
    return selected if limit is None else selected[:limit]


def summary_line(rows: Sequence[dict[str, Any]]) -> str:
    counts = Counter(row["class"] for row in rows)
    return (
        f"{len(rows)} tasks: {counts['clean']} clean, "
        f"{counts['reference_below_one']} reference_below_one, "
        f"{counts['cannot_run']} cannot_run, {counts['no_reference']} no_reference"
    )


def _excerpt(observation: AuditObservation) -> str:
    facts = observation.facts
    parts = [
        f"error: {observation.reason}" if observation.reason else "",
        f"agent:\n{facts['agent_stdout']}" if facts.get("agent_stdout") else "",
        f"verifier:\n{facts['verifier_stdout']}" if facts.get("verifier_stdout") else "",
    ]
    return _cap_excerpt("\n".join(part for part in parts if part), EXCERPT_LIMIT)


def audit_task(
    package: Path, *, corpus_root: Path, run_trial: TrialRunner, clock: Callable[[], float]
) -> dict[str, Any]:
    """One row; any per-task exception becomes a ``cannot_run`` row, except an exhausted cap."""
    from evallab.registry import task_directory_digest

    started = clock()
    row: dict[str, Any] = {
        "task": task_key(package, corpus_root),
        "package_digest": None,
        "class": "cannot_run",
        "reward": None,
        "status": "error",
        "seconds": 0.0,
        "log_excerpt": "",
        "reference_digest": None,
    }
    try:
        row["package_digest"] = task_directory_digest(package)
        solve = package / "solution" / "solve.sh"
        data = solve.read_bytes() if solve.is_file() else None
        if data is not None:
            row["reference_digest"] = f"sha256:{hashlib.sha256(data).hexdigest()}"
        if data is None or is_stub(data):
            row.update(
                {
                    "class": "no_reference",
                    "status": "not_run",
                    "log_excerpt": "solution/solve.sh is missing"
                    if data is None
                    else "solution/solve.sh is the no-solution stub",
                }
            )
        else:
            observation = run_trial(package)
            reward = finite_reward(observation.facts.get("reward"))
            row.update(
                {
                    "class": classify(reward=reward, status=observation.status),
                    "reward": reward,
                    "status": observation.status,
                    "log_excerpt": _excerpt(observation),
                }
            )
    except SpendCapReached:
        raise
    except Exception as exc:  # one task's failure must not stop the corpus
        row["log_excerpt"] = _cap_excerpt(f"{type(exc).__name__}: {exc}", EXCERPT_LIMIT)
    row["seconds"] = round(clock() - started, 1)
    return row


def render_markdown(
    rows: Sequence[dict[str, Any]], *, corpus_root: Path, today: date
) -> str:
    counts = Counter(row["class"] for row in rows)
    lines = [
        f"# Corpus reference audit, {today.isoformat()}",
        "",
        METHOD,
        "",
        f"Corpus: `{corpus_root}`",
        "",
        "| class | count |",
        "|---|---|",
        *(f"| {name} | {counts[name]} |" for name in CLASSES),
        "",
        "| task | class | reward | seconds |",
        "|---|---|---|---|",
    ]
    for row in sorted(rows, key=lambda r: r["task"]):
        reward = "" if row["reward"] is None else f"{row['reward']:g}"
        lines.append(f"| {row['task']} | {row['class']} | {reward} | {row['seconds']:g} |")
    return "\n".join(lines) + "\n"


def _write_text(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def run_corpus(
    packages: Sequence[Path],
    *,
    corpus_root: Path,
    out: Path,
    run_trial: TrialRunner,
    prior: Sequence[dict[str, Any]] = (),
    clock: Callable[[], float] = time.monotonic,
    today: date | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Audit ``packages`` after ``prior`` rows; rewrite both outputs after every task.

    Returns the rows and whether the run stopped early because the spend cap was reached.
    """
    rows = list(prior)
    stopped = False
    day = today or datetime.now(UTC).date()
    out.mkdir(parents=True, exist_ok=True)
    try:
        for package in packages:
            try:
                row = audit_task(package, corpus_root=corpus_root, run_trial=run_trial, clock=clock)
            except SpendCapReached as exc:
                print(f"qa corpus: stopped before {package.name}: {exc}", file=sys.stderr)
                stopped = True
                break
            rows.append(row)
            _write_text(out / "audit.json", json.dumps(rows, indent=2) + "\n")
    finally:
        _write_text(out / "audit.json", json.dumps(rows, indent=2) + "\n")
        _write_text(out / "AUDIT.md", render_markdown(rows, corpus_root=corpus_root, today=day))
    return rows, stopped


def load_prior(out: Path, *, resume: bool) -> list[dict[str, Any]]:
    path = out / "audit.json"
    if not path.exists():
        return []
    if not resume:
        raise ValueError(f"{path} exists; pass --resume or choose a fresh --out")
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not all(isinstance(r, dict) and "task" in r for r in rows):
        raise ValueError(f"{path} is not a list of audit rows")
    return rows


def _modal_runner(
    args: argparse.Namespace, root: Path, corpus_root: Path, out: Path
) -> TrialRunner:
    from evallab.regrade import CloudSpendGate

    try:
        from evallab.qa.task_modal import ModalBackend
    except ImportError as exc:
        raise ValueError(
            "Modal QA requires the pinned lab Harbor with its Modal extra (uv sync --extra laminar)"
        ) from exc
    if args.max_usd is None or not math.isfinite(args.max_usd) or args.max_usd <= 0:
        raise ValueError("--max-usd must be positive and finite")
    gate = CloudSpendGate(
        approval=args.approve or "",
        cap_usd=args.max_usd,
        usd_per_execution=args.usd_per_execution,
    )
    backend = ModalBackend(repo_root=root, spend_gate=gate)

    def run(package: Path) -> AuditObservation:
        task = backend.resolve(package)
        trial_dir = out / "trials" / task_key(package, corpus_root).replace("/", "__")
        observation = backend.control(
            task, "oracle", timeout_seconds=args.trial_timeout, run_dir=trial_dir
        )
        # The native path records an exhausted cap as a failed trial; it is not a task result.
        if (observation.reason or "").startswith(f"{SpendCapReached.__name__}:"):
            raise SpendCapReached(observation.reason)
        return observation

    return run


def corpus_command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    from evallab.task_import import discover_task_packages

    try:
        corpus_root = args.corpus_root.expanduser().resolve()
        out = args.out.expanduser().resolve()
        if args.limit is not None and args.limit < 1:
            raise ValueError("--limit must be at least 1")
        packages = discover_task_packages(corpus_root)
        if args.task:
            known = {task_key(p, corpus_root) for p in packages} | {p.name for p in packages}
            missing = sorted(set(args.task) - known)
            if missing:
                raise ValueError(f"unknown --task: {', '.join(missing)}")
        prior = load_prior(out, resume=args.resume)
        selected = select_tasks(
            packages,
            corpus_root=corpus_root,
            only=args.task or (),
            done=frozenset(row["task"] for row in prior),
            limit=args.limit,
        )
        run_trial = _modal_runner(args, root, corpus_root, out)
        rows, stopped = run_corpus(
            selected, corpus_root=corpus_root, out=out, run_trial=run_trial, prior=prior
        )
    except (OSError, ValueError) as exc:
        print(f"qa corpus: {exc}", file=sys.stderr)
        return 1
    print(summary_line(rows))
    return 1 if stopped else 0
