"""QA argument declarations; scoring and execution load only on dispatch."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_AGENT_LABELS = ("task", "trace", "breaker", "tripwire")
MODAL_USD_PER_ALLOCATION_UNIT = 0.0025
DEFAULT_MODEL = "openai/gpt-oss-120b"


def _score_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.qa.cli import _score_command as command

    return command(args, root, **kwargs)


def _suite_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.qa.suite import _suite_command as command

    return command(args, root, **kwargs)


def task_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.qa.task import task_command as command

    return command(args, root, **kwargs)


def score_command(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.qa.task import score_command as command

    return command(args, root, **kwargs)


def _cmd(args: argparse.Namespace, root: Path, **kwargs: Any) -> int:
    from evallab.qa.trace_cheap import _cmd as command

    return command(args, root, **kwargs)


def build_qa_parser(commands: argparse._SubParsersAction) -> None:
    """Register the ``evallab qa`` subcommand."""
    qa = commands.add_parser(
        "qa",
        help="Shared QA contract: score findings against answer keys ($0)",
        description="Score retained QA findings or compare versions on frozen answer-key splits.",
    )
    sub = qa.add_subparsers(dest="qa_cmd", required=True)
    score = sub.add_parser("score", help="Score a findings directory against an answer key")
    score.add_argument("--key", required=True, help="Answer-key id or directory path")
    score.add_argument("--findings", type=Path, required=True, help="Findings directory")
    score.add_argument(
        "--split", default=None, help="Only this split (default: all plus each split)"
    )
    score.add_argument("--agent", default="", help="Agent label (default: from findings)")
    score.add_argument("--system", default="", help="System label (default: reviewer model)")
    score.add_argument("--status", default=None, help="Status text (default: complete/partial)")
    score.add_argument("--notes", default="", help="Free-text note carried on every row")
    score.add_argument(
        "--scoreboard",
        type=Path,
        default=Path("research/qa-scoreboard/scoreboard.csv"),
        help="Scoreboard CSV to append to",
    )
    score.add_argument("--json", action="store_true")
    score.set_defaults(func=_score_command)

    add_suite_command(sub)

    add_task_commands(sub)


    add_trace_cheap_commands(sub)


def add_suite_command(commands: argparse._SubParsersAction) -> None:
    suite = commands.add_parser("suite", help="Compare retained versions on every frozen answer-key split ($0)")
    suite.add_argument(
        "--agent", choices=tuple(_AGENT_LABELS), action="append", required=True,
        help="QA agent kind; repeat to put several agents' versions in one report",
    )
    suite.add_argument(
        "--version", action="append", required=True,
        help="Exact versioned producer identity of one requested agent; repeat to compare versions",
    )
    suite.add_argument("--runs", type=Path, help="JSON runs manifest; paths are relative to its directory")
    suite.add_argument("--date", default=datetime.now(UTC).date().isoformat(), help="Report date (YYYY-MM-DD, UTC)")
    suite.add_argument("--output", type=Path, help="Atomic report destination (default: research/qa-scoreboard/suite-YYYYMMDD.md)")
    suite.add_argument("--json", action="store_true", help="Also print the full comparison and source custody as JSON")
    suite.set_defaults(func=_suite_command)


def add_task_commands(sub: Any) -> None:
    """Register ``task`` and ``task-score`` under the shared ``evallab qa`` parser."""
    task = sub.add_parser(
        "task",
        help="Executed task QA: S0 controls, advisory S1 rubric, S2 shortcut+ref-subset, S3 fresh replay",
    )
    task.add_argument("--task", type=Path, action="append")
    task.add_argument("--key")
    task.add_argument("--split", choices=("dev", "heldout"))
    task.add_argument("--item", action="append")
    task.add_argument("--packages-root", type=Path)
    task.add_argument("--run-dir", type=Path)
    task.add_argument("--stages", default="S0,S1,S2,S3")
    task.add_argument("--trial-timeout", type=int, default=1800)
    task.add_argument("--rubric", type=Path)
    task.add_argument("--model", default="codex/gpt-6-luna")
    task.add_argument("--no-model", action="store_true")
    task.add_argument("--environment", choices=("docker", "modal"), default="docker")
    task.add_argument(
        "--plan",
        action="store_true",
        help="$0 package-only execution/reservation preview; no backend or model construction",
    )
    task.add_argument("--max-usd", type=float, help="Run-wide Modal reservation cap (not a bill)")
    task.add_argument("--approve", help="Recorded approval text required for real Modal execution")
    task.add_argument(
        "--usd-per-execution",
        type=float,
        default=MODAL_USD_PER_ALLOCATION_UNIT,
        help="Positive finite USD reservation estimate per image work/native creation attempt "
        "(default: %(default)s)",
    )
    task.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Concurrent tasks: 1..16 on Modal, 1..2 on Docker (default: sequential)",
    )
    task.add_argument(
        "--evidence", type=Path, help="Existing qa.finding/v1 candidates to confirm in S3"
    )
    task.add_argument("--triplet", type=Path, help="Inert measurement triplet JSON")
    task.add_argument(
        "--mutants", type=int, default=8, help="S2 mutation hunt size (0 disables the hunt)"
    )
    task.add_argument("--ref-patch", type=Path, help="Optional reference patch for S2 ref-subset")
    task.add_argument(
        "--resume-from",
        type=Path,
        help="Prior run directory to reuse digest-bound measured checks/probes from",
    )
    task.set_defaults(func=task_command)
    score = sub.add_parser(
        "task-score",
        help="Digest-bound task-audit scores (flagged vs verified); unmeasured never counts as pass",
    )
    score.add_argument("--key", required=True)
    score.add_argument("--findings", type=Path, required=True)
    score.add_argument("--split", choices=("dev", "heldout"))
    score.add_argument("--scoreboard", type=Path)
    score.add_argument(
        "--exclude-exposed",
        action="store_true",
        help="Leave out reported-split items QA Task was tuned on or selected by "
        "(research/answer-keys/splits/exposure.jsonl); they stay in `items`, unmeasured",
    )
    score.add_argument(
        "--exclude-item", action="append", default=[], help="Also leave out this item id"
    )
    score.add_argument(
        "--level",
        choices=("verified", "flagged"),
        default="verified",
        help="Metric appended to the shared scoreboard; JSON always reports both",
    )
    score.set_defaults(func=score_command)


def add_trace_cheap_commands(sub: Any) -> None:
    """Register ``trace-cheap`` under ``evallab qa``."""
    p = sub.add_parser(
        "trace-cheap",
        help="Cheap-model trace QA: tripwires -> hackbook candidate -> cheap judge (qa.finding/v1)",
    )
    p.add_argument("target", type=Path, help="Trial dir, or its agent/trajectory.json")
    p.add_argument("--model", default=DEFAULT_MODEL, help="OpenRouter model id")
    p.add_argument("--task-id", default=None, help="Subject task id (default: trial dir name)")
    p.add_argument("--out", type=Path, default=None, help="Write the finding JSON here")
    p.add_argument(
        "--offline", action="store_true", help="No judge call: prefilter + candidates only"
    )
    p.add_argument("--json", action="store_true", help="Include mode/candidate/prefilter in output")
    p.set_defaults(func=_cmd)
