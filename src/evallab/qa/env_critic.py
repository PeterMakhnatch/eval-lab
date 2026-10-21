"""Failure-fact critic for the open QA harness (spec item 8, G9/G12).

Reads retained ``qa env audit`` sessions, attributes each session that did not end in a
confirmed finding to a harness-level failure class from recorded facts only, clusters the
sessions by cause, and *proposes* one-line facts for a human to accept into
``qa/facts/<family>.md``. No model runs; nothing is written to ``facts/`` automatically.

Failure classes (recorded status and observations, never model text):

* ``parse``       - the session stopped on ``invalid_action``.
* ``driver_error``- ``model_error``, ``harness_error`` or ``quota_stop``.
* ``no_finish``   - a time, token or tool budget ran out before a final.
* ``unmeasured``  - the session finished but every runtime-tool call was unmeasured.
* ``source_only`` - the session finished without trying execution.

``facts_for`` is the interface the lead packet (spec 4c) uses to load the accepted facts for a
task; the failed tasks of a cluster are listed DEV-only (split guard) so a fact can be tested by
re-running exactly the tasks it was written for.

    evallab qa env critic --audit-root DIR --out DIR [--min-sessions 2]
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from importlib import resources
from pathlib import Path
from typing import Any

from evallab.qa.agent_lanes import read_jsonl
from evallab.qa.env_export import (
    RUNTIME_TOOLS,
    ExportError,
    SplitGuard,
    task_name,
)

PROPOSAL_SCHEMA = "qa.fact_proposals/v1"
FAILURE_CLASSES = ("parse", "driver_error", "no_finish", "unmeasured", "source_only")
_DRIVER_STATUSES = frozenset({"model_error", "harness_error", "quota_stop"})
_BUDGET_STATUSES = frozenset(
    {"time_budget_exhausted", "token_budget_exhausted", "tool_budget_exhausted"})
_NUMBER = re.compile(r"\d[\d,._]*")
_HEX = re.compile(r"\b[0-9a-f]{12,}\b")
_PATH = re.compile(r"(?:/[\w.\-@+]+){2,}")
#: Fact lines are ``- <fact> -- evidence: <where it was measured>``; facts name their family.
_FACT_LINE = re.compile(r"-\s+(?P<fact>.+?)\s+(?:—|--)\s+evidence:\s+(?P<evidence>\S.*)")
MAX_FACT_CHARS = 400


def signature(text: Any, limit: int = 160) -> str:
    """Stable cause label: numbers, paths and long hex ids collapsed, whitespace squeezed."""
    text = " ".join(str(text or "").split())
    text = _PATH.sub("<path>", _HEX.sub("<id>", text))
    return _NUMBER.sub("N", text)[:limit]


def classify_session(agent_dir: Path) -> dict[str, Any] | None:
    """One session -> ``{class, signature, ...}`` or ``None`` when it ended confirmed."""
    agent_dir = Path(agent_dir)
    record = json.loads((agent_dir / "session.json").read_text(encoding="utf-8"))
    status = record.get("status")
    events = [e for e in read_jsonl(agent_dir / "journal.jsonl") if isinstance(e, dict)] \
        if (agent_dir / "journal.jsonl").is_file() else []
    runtime = [e for e in events if e.get("event") == "tool_call" and e.get("tool") in RUNTIME_TOOLS]
    measured = [e for e in runtime
                if (e.get("observation") or {}).get("execution_status") == "measured"]
    errors = [e for e in events if e.get("event") in ("invalid_action", "model_error", "harness_error")]
    base = {"status": status, "tool_calls": (record.get("used") or {}).get("tool_calls"),
            "runtime_calls": len(runtime), "measured_calls": len(measured)}
    if status == "invalid_action":
        return {**base, "class": "parse", "signature": signature(
            record.get("stop_reason") or (errors[-1].get("error") if errors else ""))}
    if status in _DRIVER_STATUSES:
        return {**base, "class": "driver_error", "signature": signature(
            f"{status}: {record.get('stop_reason')}")}
    if status in _BUDGET_STATUSES:
        return {**base, "class": "no_finish", "signature": signature(status)}
    if status == "final":
        if measured:
            return None
        if runtime:
            error = next(
                (((e.get("observation") or {}).get("result") or {}).get("error") for e in runtime
                 if ((e.get("observation") or {}).get("result") or {}).get("error")),
                (runtime[0].get("observation") or {}).get("execution_status"))
            return {**base, "class": "unmeasured", "signature": signature(error),
                    "tools": sorted({e["tool"] for e in runtime})}
        return {**base, "class": "source_only", "signature": "no runtime tool was called"}
    return {**base, "class": "driver_error", "signature": signature(f"status {status}")}


#: Causes that come from the host or the harness, not from the task: a prompt fact cannot fix them.
_HOST_STATE = re.compile(
    r"Docker admission|ModuleNotFoundError|No module named|unbounded CPU|zero-tool|quota|timed out|"
    r"OMP |DriverError",
    re.IGNORECASE,
)
_TEMPLATES = {
    "parse": "{n} sessions ({tasks}) stopped on invalid_action ('{sig}'): reply with exactly one "
             "JSON object, `calls` xor `final`.",
    "no_finish": "{n} sessions ({tasks}) spent a whole budget without a final (median {calls} "
                 "tool calls): return `final` or `inconclusive` at 80% of any budget.",
    "unmeasured": "{tools} cannot measure this task ('{sig}'; {n} sessions: {tasks}): record it as "
                  "unmeasured, never reward 0 or clean, and do not retry the call.",
    "source_only": "{n} sessions ({tasks}) finished without calling a runtime tool: try one "
                   "`run-reference` or `run-blank` control before relying on source reading alone.",
}
_HARNESS_TEMPLATE = (
    "{n} sessions ({tasks}) ended {klass} on host or harness state ('{sig}'): fix the host or "
    "harness before re-running; no prompt fact can remove this."
)


def route_for(klass: str, sig: str) -> str:
    """``fix_harness`` for host-state causes and driver errors, else ``fact`` for the packet."""
    return "fix_harness" if klass == "driver_error" or _HOST_STATE.search(sig) else "fact"


def _session_task(record: dict[str, Any]) -> str:
    identity = record.get("task_identity") or {}
    return task_name(str(identity.get("task_id") or Path(str(record.get("task_dir") or "?")).name))


def cluster(
    sessions: list[tuple[Path, dict[str, Any], dict[str, Any]]], *, min_sessions: int = 2
) -> list[dict[str, Any]]:
    """Group classified sessions by ``(class, signature)`` and write one proposed fact each."""
    groups: dict[tuple[str, str], list[tuple[Path, dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for item in sessions:
        groups[(item[2]["class"], item[2]["signature"])].append(item)
    proposals: list[dict[str, Any]] = []
    for (klass, sig), items in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        tasks = sorted({_session_task(i[1]) for i in items})
        if len(items) < min_sessions:
            continue
        tools = sorted({t for i in items for t in i[2].get("tools", [])})
        calls = [i[2]["tool_calls"] for i in items if isinstance(i[2].get("tool_calls"), int)]
        route = route_for(klass, sig)
        template = _HARNESS_TEMPLATE if route == "fix_harness" else _TEMPLATES[klass]
        fact = template.format(
            n=len(items), tasks=", ".join(tasks[:4]) + (", ..." if len(tasks) > 4 else ""),
            sig=sig, tools="/".join(tools) or "the runtime tools", klass=klass,
            calls=int(statistics.median(calls)) if calls else "?")
        proposals.append({
            "schema": PROPOSAL_SCHEMA, "class": klass, "signature": sig,
            "sessions": len(items), "tasks": tasks, "families": tasks, "route": route,
            "fact": fact,
            "evidence": [str(i[0]) for i in items[:5]],
        })
    return proposals


def critique(
    audit_roots: list[Path], *, min_sessions: int = 2, guard: SplitGuard | None = None
) -> dict[str, Any]:
    """Classify every session below ``audit_roots``; returns proposals and a DEV-only re-run list."""
    classified: list[tuple[Path, dict[str, Any], dict[str, Any]]] = []
    confirmed = 0
    total = 0
    for root in audit_roots:
        for session_json in sorted(Path(root).rglob("session.json")):
            if session_json.parent.name != "agent":
                continue
            try:
                record = json.loads(session_json.read_text(encoding="utf-8"))
                if not str(record.get("schema", "")).startswith("qa.env.audit/v2"):
                    continue
                verdict = classify_session(session_json.parent)
            except (OSError, ValueError):
                continue
            total += 1
            if verdict is None:
                confirmed += 1
            else:
                classified.append((session_json, record, verdict))
    proposals = cluster(classified, min_sessions=min_sessions)
    rerun: dict[str, Any] = {"tasks": [], "excluded_reported_split": []}
    if guard is not None:
        names = sorted({t for p in proposals for t in p["tasks"]})
        for name in names:
            verdict = guard.check(name)
            (rerun["tasks"] if verdict.allowed else rerun["excluded_reported_split"]).append(name)
    return {
        "schema": PROPOSAL_SCHEMA,
        "sessions": total, "ended_measured": confirmed, "failed": len(classified),
        "by_class": dict(sorted(Counter(v["class"] for _, _, v in classified).items())),
        "proposals": proposals, "rerun_dev_tasks": rerun,
    }


def render_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Proposed failure facts (for human review; nothing here is applied)",
        "",
        f"Sessions read: {result['sessions']}; ended with a measured witness: "
        f"{result['ended_measured']}; failed: {result['failed']} {result['by_class']}.",
        "",
    ]
    for route, heading in (("fact", "Proposed packet facts"), ("fix_harness", "Harness or host defects")):
        picked = [p for p in result["proposals"] if p["route"] == route]
        lines += [f"## {heading}", ""]
        lines += [
            f"- {p['fact']} — evidence: {p['sessions']} sessions, {p['evidence'][0]}" for p in picked
        ] or ["- none"]
        lines.append("")
    lines += ["DEV re-run set (same failed tasks, reported splits removed): "
              + ", ".join(result["rerun_dev_tasks"]["tasks"] or ["none"])]
    return "\n".join(lines) + "\n"


# -- accepted facts --------------------------------------------------------------------


def parse_facts(text: str, source: str = "facts") -> list[dict[str, str]]:
    """Fact lines of a ``facts/*.md`` file; a fact without evidence is refused, not guessed."""
    facts: list[dict[str, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.startswith("- "):
            continue
        match = _FACT_LINE.fullmatch(line.strip())
        if match is None:
            raise ValueError(f"{source}:{number}: a fact needs '— evidence: <where measured>'")
        if len(match["fact"]) > MAX_FACT_CHARS:
            raise ValueError(f"{source}:{number}: fact longer than {MAX_FACT_CHARS} chars")
        facts.append({"fact": match["fact"], "evidence": match["evidence"]})
    return facts


def facts_for(task_id: str) -> list[dict[str, str]]:
    """Accepted facts for a task: ``_all.md`` first, then ``<task name>.md`` if shipped."""
    root = resources.files(__package__).joinpath("facts")
    facts: list[dict[str, str]] = []
    for name in ("_all", task_name(task_id)):
        path = root.joinpath(f"{name}.md")
        if path.is_file():
            facts.extend(parse_facts(path.read_text(encoding="utf-8"), f"facts/{name}.md"))
    return facts


# -- CLI -------------------------------------------------------------------------------


def _critic_command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    try:
        guard = SplitGuard.from_repo(args.repo_root or root, scope="reported")
        result = critique(args.audit_root, min_sessions=args.min_sessions, guard=guard)
        out = Path(args.out)
        if out.exists() and any(out.iterdir()):
            raise ExportError(f"output directory is not empty: {out}")
        out.mkdir(parents=True, exist_ok=True)
        (out / "proposals.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (out / "proposals.md").write_text(render_markdown(result), encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(json.dumps({"schema": PROPOSAL_SCHEMA, "status": "setup_error",
                          "error": f"{type(exc).__name__}: {exc}"}))
        return 2
    print(json.dumps({k: v for k, v in result.items() if k != "proposals"}
                     | {"proposals": len(result["proposals"])}, indent=2, sort_keys=True))
    print(f"qa env critic: {len(result['proposals'])} proposed facts in {out}", file=sys.stderr)
    return 0


def add_critic_command(subparsers: argparse._SubParsersAction) -> None:
    """Register ``critic`` inside the ``evallab qa env`` namespace."""
    critic = subparsers.add_parser(
        "critic",
        help="Cluster failed audit sessions and propose failure facts for human review",
    )
    critic.add_argument("--audit-root", type=Path, action="append", required=True)
    critic.add_argument("--out", type=Path, required=True, help="New output directory")
    critic.add_argument("--min-sessions", type=int, default=2,
                        help="Smallest cluster that earns a proposed fact")
    critic.add_argument("--repo-root", type=Path, help="Checkout holding research/answer-keys")
    critic.set_defaults(func=_critic_command)
