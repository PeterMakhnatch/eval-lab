from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from evallab import __version__

if TYPE_CHECKING:
    from evallab.fetch import HarborBackend


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


LOCAL_ENV_KEYS = {
    "DATABASE_URL",
    "EVALLAB_DERIVED_ROOT",
    "HARBOR_CLAUDE_KEYCHAIN_ACCOUNT",
    "HARBOR_CLAUDE_KEYCHAIN_SERVICE",
    "LAB_REPORT_ISSUE",
    "POSTGRES_DB",
    "POSTGRES_PASSWORD",
    "POSTGRES_PORT",
    "POSTGRES_USER",
}


def load_local_env(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = key.strip()
        if normalized_key in LOCAL_ENV_KEYS:
            os.environ.setdefault(normalized_key, value.strip().strip("'\""))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="evallab",
        description="Run, inspect, and analyze agent evaluations through Harbor.",
    )
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)
    from evallab.cheat_parser import register_parser as register_cheat
    from evallab.database_parser import register_parser as register_database
    from evallab.detectors_parser import register_parser as register_detectors
    from evallab.docker_lock import build_docker_lock_parser as register_docker_lock
    from evallab.execution_parser import register_parser as register_execution
    from evallab.failure_library.cli import build_failures_parser as register_failures
    from evallab.hardening_parser import register_parser as register_hardening
    from evallab.laminar_parser import register_parser as register_laminar
    from evallab.line_parser import register_parser as register_line
    from evallab.operations_parser import register_parser as register_operations
    from evallab.process_job_parser import register_parser as register_process_job
    from evallab.production.parser import register_parser as register_production
    from evallab.qa.parser import build_qa_parser as register_qa
    from evallab.registry_parser import register_parser as register_registry
    from evallab.results_parser import register_parser as register_results
    from evallab.reward_hack_parser import register_parser as register_reward_hack
    from evallab.score.parser import build_score_parser as register_score
    from evallab.ship import register_parser as register_ship
    from evallab.tasks_parser import register_parser as register_tasks
    from evallab.trace_parser import register_parser as register_trace
    from evallab.view_parser import register_parser as register_view
    from evallab.watch_parser import register_parser as register_watch

    for register in (
        register_operations,
        register_execution,
        register_results,
        register_database,
        register_tasks,
        register_trace,
        register_registry,
        register_hardening,
        register_process_job,
        register_watch,
        register_reward_hack,
        register_line,
        register_score,
        register_laminar,
        register_view,
        register_detectors,
        register_cheat,
        register_qa,
        register_failures,
        register_docker_lock,
        register_production,
        register_ship,
    ):
        register(commands)
    return root


def _normalize_review_argv(argv: Sequence[str] | None) -> Sequence[str] | None:
    if argv is None:
        argv = sys.argv[1:]
    normalized: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--arm-regex" and i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            normalized.append(f"{argv[i]}={argv[i + 1]}")
            i += 2
        else:
            normalized.append(argv[i])
            i += 1
    return normalized


def run_cli(
    argv: Sequence[str] | None = None,
    *,
    workspace: Path | None = None,
    harbor: HarborBackend | None = None,
) -> int:
    root = workspace if workspace is not None else repo_root()
    load_local_env(root / ".env")
    args = parser().parse_args(_normalize_review_argv(argv))
    try:
        handler: Callable[..., int] | None = getattr(args, "func", None)
        if handler is None:
            return 2
        return handler(args, root, harbor=harbor)
    except (FileExistsError, OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def main(argv: Sequence[str] | None = None) -> None:
    raise SystemExit(run_cli(argv))


if __name__ == "__main__":
    main()
