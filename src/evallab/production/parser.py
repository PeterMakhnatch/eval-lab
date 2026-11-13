"""Production CLI declarations without database or detector imports."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

_LIVE_DATABASE = Path.home() / ".local/state/evallab-prod-live/cloud-trace-db.sqlite"
STATE_DIR = Path.home() / ".local/state/evallab-judge"
DEFAULT_SEED = "judge-v2"
DEFAULT_CONTROL_RATE = 0.0
JUDGE_DESCRIPTION = "Judge station: a human judges DEV runs blind, then sees the label and our detectors."


def _prod_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.production.cli import _command

    return _command(args, root, **kwargs)


def _run_health_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.production.run_health import _command

    return _command(args, root, **kwargs)


def _serve(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.production.judge_station import _serve as command

    return command(args, root, **kwargs)


def _export(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.production.judge_station import _export as command

    return command(args, root, **kwargs)


def _report(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.production.judge_station import _report as command

    return command(args, root, **kwargs)


def _plan(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.production.judge_station import _plan as command

    return command(args, root, **kwargs)


def _queue_options(parser: argparse.ArgumentParser) -> None:
    # Suppressed child defaults preserve options supplied before the subcommand.
    parser.add_argument(
        "--repo-root", type=Path, default=argparse.SUPPRESS,
        help="Corpus/source root (default: the current Eval Lab workspace)",
    )


def register_production_parser(commands: Any) -> None:
    """Register deterministically without opening databases, brokers, or models."""
    production = commands.add_parser(
        "prod", help="Export admitted DEV evidence",
        description=(
            "Public DEV production lane. TEST/heldout records remain sealed. "
            "All receipts are JSON."
        ),
    )
    production.set_defaults(repo_root=None)
    _queue_options(production)
    actions = production.add_subparsers(dest="prod_command", required=True)


    runs = actions.add_parser(
        "export-runs",
        help="Export blinded, registry-admitted DEV traces locally; never dispatch or upload",
    )
    runs.add_argument("--repo-root", type=Path, default=argparse.SUPPRESS)
    runs.add_argument("--source-database", type=Path, required=True)
    runs.add_argument("--output-directory", type=Path, required=True)
    runs.add_argument("--format", choices=("harbor", "atif", "scout"), required=True)
    runs.add_argument(
        "--manifest", type=Path, required=True,
        help="New sealed identity/step join file outside the export directory",
    )
    runs.add_argument(
        "--trace-id", dest="trace_ids", action="append",
        help="Further restrict admitted traces; repeat to select several",
    )
    runs.add_argument(
        "--trace-ids-file", type=Path, metavar="FILE",
        help="One trace id per line, merged with --trace-id; blank lines and # comments ignored",
    )
    runs.add_argument(
        "--strict", action="store_true",
        help="Fail before publication if any requested trace id is missing or not admitted",
    )
    runs.add_argument("--limit", type=int, help="Maximum admitted traces to export")
    runs.set_defaults(func=_prod_command)

    scope = actions.add_parser(
        "export-cluster-scope",
        help="Write a metadata-only scope file for one Run Atlas cluster; never reads run bodies",
        description=(
            "One row per DEV-admitted run in the cluster (trace id, task ids, assignments.csv digest, "
            "facet build/producer/model) plus counts of refused members. No trace bodies, labels, "
            "rewards, facet text or cluster names; the manifest must not already exist."
        ),
    )
    scope.add_argument("--repo-root", type=Path, default=argparse.SUPPRESS)
    scope.add_argument(
        "--source-database", type=Path, default=_LIVE_DATABASE,
        help="SQLite cloud trace database (default: the live store)",
    )
    scope.add_argument("--cluster-id", required=True)
    scope.add_argument("--manifest", type=Path, required=True, help="New scope file to publish")
    scope.set_defaults(func=_prod_command)

    cheatbench = actions.add_parser(
        "export-cheatbench-runs",
        help="Export blinded, registry-admitted DEV CheatBench runs that have no cloud trace; never uploads",
        description=(
            "Resolves facet run ids (cheatbench-actions-v1 item ids) through the answer-key registry's "
            "pinned published trajectories, applies the per-item DEV admission, and writes the same "
            "blinded formats as export-runs. The trial_ref and the expected-verdict fields stay in the "
            "sealed manifest outside the export."
        ),
    )
    cheatbench.add_argument("--repo-root", type=Path, default=argparse.SUPPRESS)
    cheatbench.add_argument("--output-directory", type=Path, required=True)
    cheatbench.add_argument("--format", choices=("harbor", "atif", "scout"), required=True)
    cheatbench.add_argument(
        "--manifest", type=Path, required=True, help="New sealed join file outside the export directory",
    )
    cheatbench.add_argument(
        "--run-id", dest="run_ids", action="append",
        help="Facet run id (item id) to export; repeat to select several (default: every candidate)",
    )
    cheatbench.add_argument("--limit", type=int, help="Maximum admitted runs to export")
    cheatbench.add_argument(
        "--view", choices=("full", "actions_only"), default="full",
        help="full = the trajectory the facets were produced from (default)",
    )
    cheatbench.set_defaults(func=_prod_command)


def register_run_health_parser(commands: Any) -> None:
    """``evallab run-health``; registration opens no database."""
    parser = commands.add_parser(
        "run-health", help="Run Health: deterministic loop/runaway/latency/infra features and findings",
    )
    actions = parser.add_subparsers(dest="run_health_command", required=True)
    for name, text in (("features", "per-run deterministic features (JSONL)"),
                       ("detect", "trace-findings/v1 records for the four run_health detectors (JSONL)"),
                       ("triggers", "cheap live triggers per run, for the Live Supervisor (JSONL)")):
        sub = actions.add_parser(name, help=text)
        source = sub.add_mutually_exclusive_group(required=True)
        source.add_argument("--db", type=Path, help="cloud trace DB (read-only; admitted DEV runs only)")
        source.add_argument("--trial-dir", type=Path, action="append", help="Harbor trial dir (repeatable)")
        sub.add_argument("--dataset", action="append", help="with --db: only this dataset id (repeatable)")
        sub.add_argument("--trace-id", action="append", help="with --db: only this trace id (repeatable)")
        sub.add_argument("--out", type=Path, help="output JSONL (default stdout)")
        if name == "triggers":
            sub.add_argument("--now", default="now",
                             help="clock for silence and live wall time: 'now' (default), epoch seconds or ISO")
        sub.set_defaults(func=_run_health_command)


def register_judge_parser(commands: Any) -> None:
    parser = commands.add_parser(
        "judge",
        help="Judge DEV runs yourself in a local browser page, blind, then see the label",
        description=JUDGE_DESCRIPTION,
    )
    calls = parser.add_subparsers(dest="judge_call", required=True)

    serve = calls.add_parser(
        "serve", help="Open the judging page on 127.0.0.1 (no login, no network, no models)"
    )
    serve.add_argument(
        "--queue",
        action="append",
        default=[],
        metavar="[NAME=]PATH",
        help="A queue file (repeatable)",
    )
    serve.add_argument("--judge", default=os.environ.get("EVALLAB_JUDGE", "peter"))
    serve.add_argument("--port", type=int, default=8795)
    serve.add_argument("--state-dir", type=Path, default=STATE_DIR)
    serve.add_argument(
        "--keep-order",
        action="store_true",
        help="Serve queues in file order (default: information-value order)",
    )
    serve.add_argument(
        "--blind-order",
        action="store_true",
        help="Seeded shuffle that ignores detector verdicts: the unanchored pass (default: "
        "most disputed first, which tells you early runs were alarmed or disputed)",
    )
    serve.add_argument("--seed", default=DEFAULT_SEED, help="Seed of the queue order")
    serve.add_argument(
        "--control-rate",
        type=float,
        default=DEFAULT_CONTROL_RATE,
        help="Floor on the share of early slots given to controls (default 0: spread evenly)",
    )
    serve.add_argument(
        "--no-hints", action="store_true", help="Hide the card's rule-based 'worth a look' hints"
    )
    serve.add_argument(
        "--findings",
        type=Path,
        action="append",
        default=None,
        help="Detector findings JSONL (repeatable)",
    )
    serve.add_argument(
        "--local-runs",
        type=Path,
        help="Line runs.jsonl mapping native run_id to rollout_path and sha256",
    )
    serve.add_argument("--open", action="store_true", help="Open the page in the default browser")
    serve.set_defaults(func=_serve)

    export = calls.add_parser("export", help="Write the verdicts as CSV or JSON")
    export.add_argument("--out", type=Path, required=True)
    export.add_argument("--format", choices=("csv", "json"), default="csv")
    export.add_argument("--judge", default=None)
    export.add_argument(
        "--history", action="store_true", help="Every record, not just the latest per run"
    )
    export.add_argument("--state-dir", type=Path, default=STATE_DIR)
    export.set_defaults(func=_export)

    report = calls.add_parser(
        "report", help="Agreement of your verdicts with the label, each detector and each model"
    )
    report.add_argument("--judge", default=None)
    report.add_argument("--json", action="store_true", help="Print JSON instead of markdown")
    report.add_argument("--out", type=Path, default=None, help="Write the report here too")
    report.add_argument("--state-dir", type=Path, default=STATE_DIR)
    report.set_defaults(func=_report)

    plan = calls.add_parser(
        "plan", help="Operator view of the queue order: strata per queue (spoils the blind)"
    )
    plan.add_argument("--queue", action="append", default=[], metavar="[NAME=]PATH")
    plan.add_argument("--state-dir", type=Path, default=STATE_DIR)
    plan.add_argument("--seed", default=DEFAULT_SEED)
    plan.add_argument("--control-rate", type=float, default=DEFAULT_CONTROL_RATE)
    plan.add_argument("--findings", type=Path, action="append", default=None)
    plan.add_argument(
        "--show-order", action="store_true", help="Print the stratum of every position"
    )
    plan.set_defaults(func=_plan)


def register_parser(commands: argparse._SubParsersAction) -> None:
    register_production_parser(commands)
    register_run_health_parser(commands)
    register_judge_parser(commands)
