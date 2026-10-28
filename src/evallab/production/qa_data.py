"""``evallab qa-data``: one token-budgeted JSON surface over the admitted DEV cloud trace store.

Every call answers one QA question (task bundle, run search, run window, past
issues and playbook cards, trace-tool findings) and prints a single JSON
document. Access is DEV-only through ``CloudTraceRetriever``'s admission
(registry cross-key assignments plus seals) before any content is read. A task
or run that is missing, held out, sealed or cross-key locked produces the same
``not available`` error, so a caller cannot probe which identities exist.

Tokens are estimated as ``ceil(characters / 4)`` of the compact JSON; the budget
bounds the ``data`` payload, and each section omits whole items and says how
many it omitted rather than cutting a record mid-way.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_DATABASE = Path.home() / ".local/state/evallab-prod-live/cloud-trace-db.sqlite"
DEFAULT_BUDGET = 4000
ENVELOPE_TOKENS = 120  # reserved for ``ok``/``meta`` framing around ``data``
MIN_BUDGET = 200
UNAVAILABLE = "not available: not an admitted DEV task or run"
CALLS = (
    "task", "runs", "window", "history", "findings", "inventory", "traces", "clusters", "recount",
    "split", "export", "steps", "scan",
)
NO_STORE_CALLS = frozenset({"split", "steps", "scan"})


class QaDataError(ValueError):
    """A request the tool refuses; the message never says why an identity is unavailable."""


def estimate_tokens(value: Any) -> int:
    """``ceil(chars / 4)`` of the compact JSON (or the text itself)."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return math.ceil(len(text) / 4)


@dataclass
class Budget:
    """Token allowance for one call's ``data``; sections take whole items from it."""

    limit: int
    used: int = 0
    omitted: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def take(self, item: Any) -> bool:
        """Charge ``item`` if it fits; otherwise count it as omitted and return False."""
        cost = estimate_tokens(item)
        if cost > self.remaining:
            self.omitted += 1
            return False
        self.used += cost
        return True

    def take_text(self, text: str) -> tuple[str, bool]:
        """Charge as much of ``text`` as fits (cut on a character boundary); flag a cut."""
        room = self.remaining * 4
        if len(text) <= room:
            self.used += estimate_tokens(text)
            return text, False
        self.used += self.remaining
        return text[:room], True


def bare_task_name(task_id: str) -> str:
    """The name cohort and ledger files use: no namespace, version pin or ``__`` prefix, case-folded."""
    return task_id.split("@", 1)[0].rsplit("/", 1)[-1].rsplit("__", 1)[-1].casefold()


def match_admitted_task(admitted: Any, wanted: str) -> str | None:
    """An exact admitted id, else (for a BARE name only) the single admitted id with that bare name.

    A qualified input (namespace ``/``, version ``@`` or ``__`` prefix) names one identity and
    matches only exactly: a denied ``test/alpha`` must never be served by an admitted ``dev/alpha``.
    Several admitted ids sharing the bare name refuse with a short admitted-only candidate list;
    no admitted match is the uniform refusal at the call site.
    """
    if wanted in admitted:
        return wanted
    if any(mark in wanted for mark in ("/", "@", "__")):
        return None
    matches = sorted(task for task in admitted if bare_task_name(task) == bare_task_name(wanted))
    if len(matches) > 1:
        shown = ", ".join(task[:80] for task in matches[:3])
        raise QaDataError(f"{wanted[:60]!r} matches {len(matches)} admitted tasks; give the full id, e.g. {shown}")
    return matches[0] if matches else None


@dataclass
class QaContext:
    """Open read-only store, retriever and budget shared by the call implementations.

    Admission is never cached: every ``require_task`` / ``task_of_run`` decides it
    afresh inside the same read snapshot as the row it then returns, so a task
    reclassified between two reads cannot be authorized by an earlier decision.
    """

    database: Any
    retriever: Any
    repo_root: Path
    state_dir: Path
    budget: Budget
    database_path: str | None = None  # the SQLite path/URL this context opened (for readers that reopen it)

    def admitted_task_ids(self) -> frozenset[str]:
        """Registry-admitted DEV task ids now (own snapshot; do not call inside a transaction)."""
        with self.database._transaction():
            admitted, _sealed = self.retriever._admitted_ids()
        return frozenset(admitted)

    def require_task(self, task_id: str) -> dict[str, Any]:
        """The stored row of an admitted task, admitted and read in one snapshot, else the uniform refusal.

        ``task_id`` may be the full namespaced id or the bare name cohort and ledger files use
        (``cad-model`` for ``terminal-bench/cad-model``): an exact admitted id wins, otherwise
        the one admitted task with that bare name. Use the returned row's ``task_id`` afterwards.
        """
        if not isinstance(task_id, str):
            raise QaDataError(UNAVAILABLE)
        with self.database._transaction():
            admitted, _sealed = self.retriever._admitted_ids()
            canonical = match_admitted_task(admitted, task_id)
            rows = (
                self.database._rows("SELECT * FROM corpus_tasks WHERE task_id = ?", (canonical,))
                if canonical is not None else []
            )
        if not rows:
            raise QaDataError(UNAVAILABLE)
        return rows[0]

    def task_of_run(self, run_id: str) -> str:
        """The task owning an admitted run (``agent_traces.trace_id``), else the uniform refusal."""
        if not isinstance(run_id, str):
            raise QaDataError(UNAVAILABLE)
        with self.database._transaction():
            admitted, _sealed = self.retriever._admitted_ids()
            row = self.database._execute(
                "SELECT task_id FROM agent_traces WHERE trace_id = ?", (run_id,),
            ).fetchone()
        if row is None or row[0] not in admitted:
            raise QaDataError(UNAVAILABLE)
        return row[0]


def _parse_facets(items: Sequence[str]) -> dict[str, Any]:
    """``key=v1,v2`` pairs to a facet mapping; numbers and booleans are typed."""
    facets: dict[str, Any] = {}
    for item in items:
        key, separator, raw = item.partition("=")
        if not separator or not key.strip() or not raw.strip():
            raise QaDataError(f"--facet must look like key=value[,value]; got {item!r}")
        values: list[Any] = []
        for part in raw.split(","):
            part = part.strip()
            values.append(
                True if part == "true" else False if part == "false"
                else int(part) if part.lstrip("-").isdigit() else part
            )
        facets[key.strip()] = values[0] if len(values) == 1 else values
    return facets


def open_context(
    cloud_database: str | Path = DEFAULT_DATABASE, *, repo_root: Path | None = None, budget: int = DEFAULT_BUDGET,
) -> QaContext:
    """A read-only context over ``cloud_database`` with its own token budget (caller closes ``.database``)."""
    from evallab.production.cloud_trace_db import CloudTraceDatabase, CloudTraceRetriever
    from evallab.production.corpus_queue import CorpusRegistry

    root = Path(repo_root).resolve() if repo_root else Path.cwd().resolve()
    database = CloudTraceDatabase(cloud_database, read_only=True)
    retriever = CloudTraceRetriever(database, registry=CorpusRegistry(root))
    return QaContext(
        database=database, retriever=retriever, repo_root=root,
        state_dir=Path(os.environ.get("EVALLAB_PROD_STATE") or DEFAULT_DATABASE.parent).expanduser(),
        budget=Budget(max(1, budget - ENVELOPE_TOKENS)),
        database_path=str(cloud_database),
    )


def _open(args: argparse.Namespace, budget: Budget) -> QaContext:
    context = open_context(args.cloud_database, repo_root=getattr(args, "repo_root", None))
    context.budget = budget
    return context


def _dispatch_without_store(call: str, args: argparse.Namespace, budget: Budget) -> dict[str, Any]:
    """Calls that never open the trace store: split lookup, step maps and local scanners."""
    root = Path(args.repo_root).resolve() if getattr(args, "repo_root", None) else Path.cwd().resolve()
    if call == "split":
        from evallab.production.qa_data_split import split

        return split(root, args.mode, args.ids)
    if call == "steps":
        from evallab.production.qa_data_export import steps

        return steps(args.manifest, args.trace, original=args.original, blinded=args.blinded)
    from evallab.production.qa_data_scan import scan

    return scan(args.transcripts, args.scanner, budget)


def _dispatch(call: str, context: QaContext, args: argparse.Namespace) -> dict[str, Any]:
    """Import only the module the requested call needs."""
    if call == "task":
        from evallab.production.qa_data_task import task

        return task(context, args.task_id, files=args.file, variant=args.variant, kind=args.kind)
    if call == "findings":
        from evallab.production import qa_data_findings

        if args.run is None and args.task is None:
            return qa_data_findings.summary(context)
        return qa_data_findings.findings(context, run_id=args.run, task_id=args.task, limit=args.limit)
    if call in {"inventory", "traces", "clusters", "recount"}:
        from evallab.production import qa_data_inventory

        if call == "inventory":
            return qa_data_inventory.inventory(context, ids_out=args.ids_out)
        if call == "traces":
            return qa_data_inventory.traces(
                context, task_id=args.task, dataset=args.dataset, model=args.model, outcome=args.outcome,
                limit=args.limit, ids_only=args.ids_only,
            )
        if call == "clusters":
            return qa_data_inventory.clusters(context, members=args.members, source=args.source, level=args.level)
        return qa_data_inventory.recount(context)
    if call == "export":
        from evallab.production.qa_data_export import export

        return export(
            context, facets=_parse_facets(args.facet), trace_ids=args.trace_id, ids_file=args.ids_file,
            limit=args.limit, format=args.format, out=args.out,
        )
    from evallab.production import qa_data_calls

    if call == "runs":
        return qa_data_calls.runs(
            context, facets=_parse_facets(args.facet), cluster_id=args.cluster, like_run_id=args.like,
            query=args.query, task_id=args.task, limit=args.limit,
        )
    if call == "window":
        return qa_data_calls.window(
            context, args.run_id, first=args.first, last=args.last, around=args.around, radius=args.radius,
        )
    if call == "history":
        return qa_data_calls.history(context, args.task_id, flags=args.flag, limit=args.limit)
    raise QaDataError(f"unknown call {call!r}")


def run(args: argparse.Namespace, root: Path | None = None, *, harbor: Any = None, out: Any = None) -> int:
    """Execute one call and print its JSON envelope; refusals exit 2 with the same envelope shape.

    ``root`` and ``harbor`` follow the CLI handler convention; the repository root is
    ``--repo-root`` or the directory the CLI was started from.
    """
    out = out or sys.stdout
    call = args.qa_data_call
    pretty = bool(getattr(args, "pretty", False))
    started = time.perf_counter()
    if args.budget < MIN_BUDGET:
        return _reply(out, call, args.budget, started, None, f"--budget must be at least {MIN_BUDGET}", 0, pretty)
    room = args.budget - ENVELOPE_TOKENS
    budget = Budget(room)
    context: QaContext | None = None
    data: dict[str, Any] | None = None
    error: str | None = None
    try:
        if call in NO_STORE_CALLS:
            data = _dispatch_without_store(call, args, budget)
        else:
            context = _open(args, budget)
            data = _dispatch(call, context, args)
        needed = estimate_tokens(data)
        if needed > room:  # a section failed to charge something it returned
            data, error = None, f"this call needs about {needed} tokens for its smallest answer; raise --budget"
    except QaDataError as exc:
        data, error = None, str(exc)
    except sqlite3.OperationalError as exc:
        data, error = None, f"the store cannot answer this call: {str(exc)[:120]}"
    finally:
        if context is not None:
            context.database.close()
    return _reply(out, call, args.budget, started, data, error, budget.omitted, pretty)


def _reply(
    out: Any, call: str, limit: int, started: float, data: dict[str, Any] | None, error: str | None,
    omitted: int, pretty: bool = False,
) -> int:
    meta: dict[str, Any] = {
        "call": call, "budget": limit, "omitted_items": omitted,
        "truncated": omitted > 0 or bool(data and data.get("truncated")), "tokens_est": 0, "latency_ms": 0.0,
    }
    payload: dict[str, Any] = {"ok": error is None, **({"data": data} if error is None else {"error": error})}
    payload["meta"] = meta
    meta["tokens_est"] = estimate_tokens(payload)
    meta["tokens_est"] = estimate_tokens(payload)  # the count's own digits can add a character
    meta["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    if pretty:
        print(json.dumps(payload, ensure_ascii=False, indent=1), file=out)
    else:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), file=out)
    return 0 if error is None else 2


def register_parser(commands: Any) -> None:
    """Register ``qa-data`` without opening any database."""
    from evallab.production.qa_data_guide import GUIDE

    parser = commands.add_parser(
        "qa-data", help="Token-budgeted JSON data access for QA agents over the admitted DEV trace store",
        description=__doc__.split("\n\n")[0], epilog=GUIDE, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    calls = parser.add_subparsers(dest="qa_data_call", required=True)

    def common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--repo-root", type=Path, default=None)
        sub.add_argument("--cloud-database", default=str(DEFAULT_DATABASE), help="SQLite path/URL or PostgreSQL URL")
        sub.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help="Approximate token cap for the reply")
        sub.add_argument("--pretty", action="store_true", help="Indent the JSON (one field per line) for readers that clip long lines")
        sub.set_defaults(func=run)

    task = calls.add_parser("task", help="Instruction, tests, grader and Dockerfile of one admitted task")
    common(task)
    task.add_argument("task_id")
    task.add_argument("--file", action="append", default=[], metavar="REL", help="Also return this task file (repeatable)")
    task.add_argument("--variant", type=int, default=0, help="Task variant for package listings and --file (default 0)")
    task.add_argument(
        "--kind", choices=("original_package", "repaired_package"), default="original_package",
        help="Which package of the variant --file reads",
    )

    runs = calls.add_parser("runs", help="Search runs by facets, cluster and similarity")
    common(runs)
    runs.add_argument("--facet", action="append", default=[], metavar="KEY=V[,V]")
    runs.add_argument("--cluster", default=None, metavar="CLUSTER_ID")
    runs.add_argument("--like", default=None, metavar="RUN_ID", help="Rank by similarity to this admitted run")
    runs.add_argument("--query", default="", help="Lexical terms")
    runs.add_argument("--task", default=None, metavar="TASK_ID", help="Only runs of this task")
    runs.add_argument("--limit", type=int, default=10)

    window = calls.add_parser("window", help="Steps of one run (a range, or a radius around one step)")
    common(window)
    window.add_argument("run_id")
    window.add_argument("--first", type=int, default=None)
    window.add_argument("--last", type=int, default=None)
    window.add_argument("--around", type=int, default=None)
    window.add_argument("--radius", type=int, default=3)

    history = calls.add_parser("history", help="Past confirmed issues, similar examples and playbook cards")
    common(history)
    history.add_argument("task_id")
    history.add_argument("--flag", action="append", default=[], help="Trigger flag for playbook cards (repeatable)")
    history.add_argument("--limit", type=int, default=5)

    findings = calls.add_parser("findings", help="Scout, Laminar and Docent findings for one run or task")
    common(findings)
    findings.add_argument("--run", default=None, metavar="RUN_ID")
    findings.add_argument("--task", default=None, metavar="TASK_ID")
    findings.add_argument("--limit", type=int, default=20)

    inventory = calls.add_parser("inventory", help="Counts of admitted tasks and runs by dataset, outcome and model")
    common(inventory)
    inventory.add_argument("--ids-out", type=Path, default=None, metavar="FILE", help="Also write every admitted trace id here (must not exist)")

    traces = calls.add_parser("traces", help="List admitted runs (faceted or not) by task, dataset, model or outcome")
    common(traces)
    traces.add_argument("--task", default=None, metavar="TASK_ID", help="Full id or bare name (cad-model)")
    traces.add_argument("--dataset", default=None)
    traces.add_argument("--model", default=None)
    traces.add_argument("--outcome", default=None, choices=("pass", "fail", "unknown"), help="From the verifier reward")
    traces.add_argument("--limit", type=int, default=20)
    traces.add_argument("--ids-only", action="store_true")

    clusters = calls.add_parser("clusters", help="Run Atlas clusters: the database view or the latest build file")
    common(clusters)
    clusters.add_argument("--members", default=None, metavar="CLUSTER_ID", help="Admitted member ids of one cluster")
    clusters.add_argument("--source", choices=("db", "file"), default="db")
    clusters.add_argument("--level", type=int, choices=(1, 2, 3), default=None, help="List only this level")

    recount = calls.add_parser("recount", help="Facets: the interim JSONL against the database, over admitted runs")
    common(recount)

    split = calls.add_parser("split", help="DEV, TEST or UNKNOWN for a task id or a (key, item) pair; never a label")
    common(split)
    split.add_argument("mode", choices=("task", "item"))
    split.add_argument("ids", nargs="+", metavar="ID", help="task: TASK_ID (full or bare); item: KEY_ID ITEM_ID")

    export = calls.add_parser("export", help="Blinded export of admitted runs chosen by facets or ids, with the step map")
    common(export)
    export.add_argument("--facet", action="append", default=[], metavar="KEY=V[,V]", help="Select runs by facet filter (most suspicious first)")
    export.add_argument("--trace-id", action="append", default=[], metavar="ID", help="Select runs by id (repeatable)")
    export.add_argument("--ids-file", type=Path, default=None, metavar="FILE", help="One trace id per line")
    export.add_argument("--limit", type=int, default=None, help="At most this many runs")
    export.add_argument("--format", choices=("atif", "harbor", "scout"), default="atif")
    export.add_argument("--out", type=Path, required=True, help="NEW directory; the sealed manifest goes beside it as <name>.sealed-manifest.json")

    steps = calls.add_parser("steps", help="Map original step ids to exported step numbers (and back) with a sealed manifest")
    common(steps)
    steps.add_argument("manifest", type=Path)
    steps.add_argument("trace", help="Trace id or blind id")
    which = steps.add_mutually_exclusive_group()
    which.add_argument("--original", type=int, default=None)
    which.add_argument("--blinded", type=int, default=None)

    scan = calls.add_parser("scan", help="Deterministic Scout text scanners over a blinded scout export (no model call)")
    common(scan)
    scan.add_argument("transcripts", type=Path, help="transcripts.jsonl from `export --format scout`")
    scan.add_argument("--scanner", action="append", default=None, help="Default: all four text scanners")


__all__ = [
    "CALLS", "DEFAULT_BUDGET", "MIN_BUDGET", "UNAVAILABLE", "Budget", "QaContext", "QaDataError",
    "estimate_tokens", "register_parser", "run",
]

