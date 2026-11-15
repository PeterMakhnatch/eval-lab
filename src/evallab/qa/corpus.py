"""Model-free corpus audit: a reference run and a do-nothing control per task (``evallab qa corpus``).

Each task's own ``solution/solve.sh`` (``oracle``) and an empty run (``nop``) execute
once each through the native Harbor Modal trial path (``ModalBackend.control``) under one
run-wide cloud spend gate. No agent, model, candidate or mutation stage runs. Rows are
classified from the observed rewards and trial statuses only.
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

from evallab.dataset_audit_contracts import AuditObservation, AuditTask
from evallab.qa.task import _cap_excerpt, finite_reward
from evallab.regrade import SpendCapReached

# Report order: the most important finding first.
CLASSES = (
    "control_paid",
    "reference_below_one",
    "reference_unmeasured",
    "cannot_run",
    "no_reference",
    "clean",
)
STUB_SHA256_PREFIX = "4e25c8e51ff8"
STUB_TEXT = "no solution written"
EXCERPT_LIMIT = 4_000

# Runs one native trial of ``package`` as ``"oracle"`` or ``"nop"``.
TrialRunner = Callable[[Path, str], AuditObservation]
# Builds the command that reproduces a table holding exactly these task keys.
CommandBuilder = Callable[[Sequence[str]], str]


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


def _cell(status: str, reward: float | None = None, seconds: float = 0.0) -> dict[str, Any]:
    return {"reward": reward, "status": status, "seconds": seconds}


def _measured(cell: dict[str, Any]) -> bool:
    return cell["status"] == "executed" and cell["reward"] is not None


def classify(*, oracle: dict[str, Any], nop: dict[str, Any]) -> str:
    """Class of a task whose reference ran; ``no_reference`` is decided before running.

    A ``nop`` cell with status ``skipped`` (``--skip-controls``) leaves the oracle alone to
    decide. With controls on, an oracle reward of 1 is ``clean`` only when the nop control
    measured 0; an unmeasured control keeps the task ``cannot_run``.
    """
    oracle_ok, nop_ok = _measured(oracle), _measured(nop)
    if not oracle_ok and not nop_ok:
        return "cannot_run"
    if nop_ok and nop["reward"] > 0:
        return "control_paid"
    if not oracle_ok:
        return "reference_unmeasured"
    if oracle["reward"] < 1:
        return "reference_below_one"
    return "clean" if nop_ok or nop["status"] == "skipped" else "cannot_run"


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


def summary_line(rows: Sequence[dict[str, Any]], *, controls: bool = True) -> str:
    counts = Counter(row["class"] for row in rows)
    line = f"{len(rows)} tasks: " + ", ".join(f"{counts[name]} {name}" for name in CLASSES)
    return line if controls else f"{line}; controls skipped"


def _excerpt(observation: AuditObservation) -> str:
    facts = observation.facts
    parts = [
        f"error: {observation.reason}" if observation.reason else "",
        f"agent:\n{facts['agent_stdout']}" if facts.get("agent_stdout") else "",
        f"verifier:\n{facts['verifier_stdout']}" if facts.get("verifier_stdout") else "",
    ]
    return _cap_excerpt("\n".join(part for part in parts if part), EXCERPT_LIMIT)


def _run_cell(
    package: Path, agent: str, *, run_trial: TrialRunner, clock: Callable[[], float]
) -> tuple[dict[str, Any], str]:
    """One native trial; its exception becomes an ``error`` cell, except an exhausted cap."""
    started = clock()
    try:
        observation = run_trial(package, agent)
    except SpendCapReached:
        raise
    except Exception as exc:  # one cell's failure must not stop the task or the corpus
        cell = _cell("error")
        excerpt = _cap_excerpt(f"{type(exc).__name__}: {exc}", EXCERPT_LIMIT)
    else:
        cell = _cell(observation.status, finite_reward(observation.facts.get("reward")))
        excerpt = _excerpt(observation)
    cell["seconds"] = round(clock() - started, 1)
    return cell, excerpt


def audit_task(
    package: Path,
    *,
    corpus_root: Path,
    run_trial: TrialRunner,
    clock: Callable[[], float],
    controls: bool = True,
) -> dict[str, Any]:
    """One row: oracle, then nop unless ``controls`` is off. A cap stop writes no row."""
    from evallab.registry import task_directory_digest

    started = clock()
    row: dict[str, Any] = {
        "task": task_key(package, corpus_root),
        "package_digest": None,
        "class": "cannot_run",
        "seconds": 0.0,
        "log_excerpt": "",
        "oracle": _cell("not_run"),
        "nop": _cell("not_run" if controls else "skipped"),
        "reference_digest": None,
    }
    try:
        row["package_digest"] = task_directory_digest(package)
        solve = package / "solution" / "solve.sh"
        data = solve.read_bytes() if solve.is_file() else None
        if data is not None:
            row["reference_digest"] = f"sha256:{hashlib.sha256(data).hexdigest()}"
        if data is None or is_stub(data):
            row["class"] = "no_reference"
            row["log_excerpt"] = (
                "solution/solve.sh is missing"
                if data is None
                else "solution/solve.sh is the no-solution stub"
            )
        else:
            oracle, oracle_excerpt = _run_cell(package, "oracle", run_trial=run_trial, clock=clock)
            nop, nop_excerpt = (
                _run_cell(package, "nop", run_trial=run_trial, clock=clock)
                if controls
                else (row["nop"], "")
            )
            klass = classify(oracle=oracle, nop=nop)
            nop_decides = klass == "control_paid" or (klass == "cannot_run" and _measured(oracle))
            row.update(
                {
                    "class": klass,
                    "log_excerpt": nop_excerpt if nop_decides else oracle_excerpt,
                    "oracle": oracle,
                    "nop": nop,
                }
            )
    except SpendCapReached:
        raise
    except Exception as exc:  # one task's failure must not stop the corpus
        row["log_excerpt"] = _cap_excerpt(f"{type(exc).__name__}: {exc}", EXCERPT_LIMIT)
    row["seconds"] = round(clock() - started, 1)
    return row


def _reward(cell: dict[str, Any]) -> str:
    return "" if cell["reward"] is None else f"{cell['reward']:g}"


def render_markdown(
    rows: Sequence[dict[str, Any]],
    *,
    corpus_root: Path,
    today: date,
    command: str,
    controls: bool = True,
) -> str:
    counts = Counter(row["class"] for row in rows)
    control = "an empty run (nop)" if controls else "no nop control (--skip-controls)"
    lines = [
        f"# Corpus reference audit, {today.isoformat()}",
        "",
        f"Method: each task's own solution/solve.sh (oracle) and {control} ran once each in a "
        "fresh native Harbor Modal trial with the task's own verifier; no agent, model, "
        "candidate or mutation stage ran.",
        "",
        f"Corpus: `{corpus_root}`",
        "",
        "Reproduce:",
        "",
        "```sh",
        command,
        "```",
        "",
        "| class | count |",
        "|---|---|",
        *(f"| {name} | {counts[name]} |" for name in CLASSES),
        "",
        "| task | oracle reward | nop reward | class | seconds |",
        "|---|---|---|---|---|",
    ]
    for row in sorted(rows, key=lambda r: (CLASSES.index(r["class"]), r["task"])):
        lines.append(
            f"| {row['task']} | {_reward(row['oracle'])} | {_reward(row['nop'])} "
            f"| {row['class']} | {row['seconds']:g} |"
        )
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
    command: CommandBuilder,
    prior: Sequence[dict[str, Any]] = (),
    controls: bool = True,
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
                row = audit_task(
                    package,
                    corpus_root=corpus_root,
                    run_trial=run_trial,
                    clock=clock,
                    controls=controls,
                )
            except SpendCapReached as exc:
                print(f"qa corpus: stopped before {package.name}: {exc}", file=sys.stderr)
                stopped = True
                break
            rows.append(row)
            _write_text(out / "audit.json", json.dumps(rows, indent=2) + "\n")
    finally:
        _write_text(out / "audit.json", json.dumps(rows, indent=2) + "\n")
        report = render_markdown(
            rows,
            corpus_root=corpus_root,
            today=day,
            command=command([row["task"] for row in rows]),
            controls=controls,
        )
        _write_text(out / "AUDIT.md", report)
    return rows, stopped


def _current(row: dict[str, Any], *, controls: bool) -> bool:
    """A row is reusable only in the oracle-and-nop schema, with a nop cell if controls are on."""
    nop = row.get("nop")
    return isinstance(nop, dict) and not (controls and nop.get("status") == "skipped")


def load_prior(out: Path, *, resume: bool, controls: bool = True) -> list[dict[str, Any]]:
    """Rows to keep. Older-schema rows move to ``audit.superseded.json`` and their tasks re-run."""
    path = out / "audit.json"
    if not path.exists():
        return []
    if not resume:
        raise ValueError(f"{path} exists; pass --resume or choose a fresh --out")
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or not all(isinstance(r, dict) and "task" in r for r in rows):
        raise ValueError(f"{path} is not a list of audit rows")
    stale = [row for row in rows if not _current(row, controls=controls)]
    if stale:
        superseded = out / "audit.superseded.json"
        kept = json.loads(superseded.read_text(encoding="utf-8")) if superseded.exists() else []
        _write_text(superseded, json.dumps([*kept, *stale], indent=2) + "\n")
    return [row for row in rows if _current(row, controls=controls)]


def reproduce_command(
    args: argparse.Namespace, *, corpus_root: Path, out: Path, tasks: Sequence[str]
) -> str:
    """The ``evallab qa corpus`` call that rebuilds these rows into a fresh ``--out``."""
    parts = ["uv", "run", "evallab", "qa", "corpus", str(corpus_root), "--out", str(out)]
    for task in tasks:
        parts += ["--task", task]
    if args.skip_controls:
        parts.append("--skip-controls")
    parts += [
        "--trial-timeout",
        str(args.trial_timeout),
        "--max-usd",
        str(args.max_usd),
        "--approve",
        args.approve or "",
        "--usd-per-execution",
        str(args.usd_per_execution),
    ]
    return shlex.join(parts)


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
    resolved: dict[Path, AuditTask] = {}

    def run(package: Path, agent: str) -> AuditObservation:
        if agent not in {"oracle", "nop"}:
            raise ValueError(f"unknown control agent: {agent}")
        if package not in resolved:  # both cells of a task share one resolution
            resolved.clear()
            resolved[package] = backend.resolve(package)
        trial_dir = out / "trials" / task_key(package, corpus_root).replace("/", "__")
        observation = backend.control(
            resolved[package],
            "oracle" if agent == "oracle" else "nop",
            timeout_seconds=args.trial_timeout,
            run_dir=trial_dir,
        )
        # The native path records an exhausted cap as a failed trial; it is not a task result.
        if (observation.reason or "").startswith(f"{SpendCapReached.__name__}:"):
            raise SpendCapReached(observation.reason)
        return observation

    return run


def corpus_command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    from evallab.task_import import discover_task_packages

    controls = not args.skip_controls
    try:
        corpus_root = args.corpus_root.expanduser().resolve()
        out = args.out.expanduser().resolve()
        if args.limit is not None and args.limit < 1:
            raise ValueError("--limit must be at least 1")
        packages = discover_task_packages(corpus_root)
        all_keys = {task_key(p, corpus_root) for p in packages}
        if args.task:
            missing = sorted(set(args.task) - all_keys - {p.name for p in packages})
            if missing:
                raise ValueError(f"unknown --task: {', '.join(missing)}")
        run_trial = _modal_runner(args, root, corpus_root, out)
        prior = load_prior(out, resume=args.resume, controls=controls)
        selected = select_tasks(
            packages,
            corpus_root=corpus_root,
            only=args.task or (),
            done=frozenset(row["task"] for row in prior),
            limit=args.limit,
        )

        def command(tasks: Sequence[str]) -> str:
            listed = () if set(tasks) == all_keys else sorted(tasks)
            return reproduce_command(args, corpus_root=corpus_root, out=out, tasks=listed)

        rows, stopped = run_corpus(
            selected,
            corpus_root=corpus_root,
            out=out,
            run_trial=run_trial,
            command=command,
            prior=prior,
            controls=controls,
        )
    except (OSError, ValueError) as exc:
        print(f"qa corpus: {exc}", file=sys.stderr)
        return 1
    print(summary_line(rows, controls=controls))
    return 1 if stopped else 0
