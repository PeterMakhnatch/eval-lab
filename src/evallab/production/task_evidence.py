"""Read-only QA evidence for one admitted DEV task, assembled from retained records.

Everything known about a task in one view, and nothing recomputed:

* the task's identity and grader digests (never the instruction text);
* every run of the task with its Run Atlas facet, cluster path and the findings of the
  whole-run trace tools (``trace-tools/results-v1.jsonl``);
* Task QA: the retained ``task_issues`` rows plus the journal's ``qa.finding/v1`` and
  blocked events;
* Reward Seeker differentials: the journal's per-attempt grader feedback and confirmed
  exploits (counts, rewards and one-line mechanisms; exploit files are never returned);
* grader fixes: the recorded repaired-grader digest and the journal's qualified repairs;
* copyable ``evallab`` and ``curl`` commands an agent can run to fetch the same evidence.

The caller supplies already-admitted identities; this module opens no database, calls no
model or network, joins no label and writes nothing. A source that is missing says so; an
absent finding is "none recorded", never a clean bill.
"""

from __future__ import annotations

import re
import shlex
import threading
from collections import defaultdict
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

from evallab.production.atlas_view import strict_loads
from evallab.production.tool_scorecard_view import iter_lines, parse_moment

SCHEMA = "evallab.task_evidence/v1"
JOURNAL_EVENT_TYPES = (
    "seeker.grader_feedback",
    "seeker.exploit_confirmed",
    "worker.completed",
    "qa.finding/v1",
    "task_quality.blocked",
    "task_quality.repair_qualified",
)
MAX_RUNS = 100
MAX_EXPORT_IDS = 10
MAX_TEXT = 300
SEARCH_LIMIT = 25
RESULTS_FILE = "results-v1.jsonl"
_BATCH = 500
_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


def _text(value: Any, limit: int = MAX_TEXT) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _event_base(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "seq": _count(event.get("seq")),
        "at": _text(event.get("ts"), 40),
        "campaign": _text(event.get("run_id"), 120),
    }


def _seeker_attempt(event: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **_event_base(event),
        "kind": "attempt",
        "attempt_index": _count(payload.get("attempt_index")),
        "differential_confirmed": payload.get("differential_confirmed") is True,
        "original_reward": _number(payload.get("original_reward")),
        "repaired_reward": _number(payload.get("repaired_reward")),
        "failure_kind": _text(payload.get("failure_kind"), 80),
        "taxonomy_class": _text(payload.get("taxonomy_class"), 80),
        "hackbook_entry_id": _text(payload.get("hackbook_entry_id"), 60),
        "target_check": _text(payload.get("target_check"), 200),
        "model_route": _text(payload.get("model_route"), 120),
        "status": _text(event.get("status"), 30),
    }


def _seeker_confirmed(event: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **_event_base(event),
        "kind": "confirmed",
        "attempt_index": _count(payload.get("attempt_index")),
        "differential_confirmed": True,
        "original_reward": _number(payload.get("original_reward")),
        "repaired_reward": _number(payload.get("repaired_reward")),
        "taxonomy_class": _text(payload.get("taxonomy_class"), 80),
        "hackbook_entry_id": _text(payload.get("hackbook_entry_id"), 60),
        "mechanism": _text(payload.get("mechanism"), 240),
        "model_route": _text(payload.get("model_route"), 120),
        "status": _text(event.get("status"), 30),
    }


def _qa_finding(event: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **_event_base(event),
        "kind": "finding",
        "finding_id": _text(payload.get("finding_id"), 80),
        "phase": _text(payload.get("phase"), 60),
        "severity": _text(payload.get("severity"), 30),
        "status": _text(payload.get("status"), 30),
        "direction": _text(payload.get("direction"), 30),
        "summary": _text(payload.get("summary")),
    }


def _qa_blocked(event: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    ids = payload.get("finding_ids")
    return {
        **_event_base(event),
        "kind": "blocked",
        "status": _text(payload.get("status"), 30),
        "finding_ids": [item[:80] for item in ids if isinstance(item, str)][:10]
        if isinstance(ids, list)
        else [],
    }


def _repair(event: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    rewards = payload.get("control_rewards")
    return {
        **_event_base(event),
        "kind": "repair",
        "qualified": payload.get("qualified") is True,
        "status": _text(payload.get("status"), 30),
        "source": _text(payload.get("source"), 80),
        "repaired_task_digest": _text(payload.get("repaired_task_digest"), 90),
        "exploit": _count(payload.get("exploit")),
        "exploit_trials": _count(payload.get("exploit_trials")),
        "nop": _count(payload.get("nop")),
        "nop_trials": _count(payload.get("nop_trials")),
        "ref_passes": _count(payload.get("ref_passes")),
        "reference_trials": _count(payload.get("reference_trials")),
        "control_rewards": {
            str(name)[:40]: _number(value)
            for name, value in list(rewards.items())[:6]
            if _number(value) is not None
        }
        if isinstance(rewards, dict)
        else {},
    }


def _classify(event: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The section and bounded record for one journal event, or None if it is not evidence."""
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return None
    kind, system = event.get("event_type"), event.get("system")
    if kind == "seeker.grader_feedback":
        return "seeker", _seeker_attempt(event, payload)
    if kind == "seeker.exploit_confirmed":
        return "seeker", _seeker_confirmed(event, payload)
    if kind == "worker.completed" and system == "reward_seeker":
        return "seeker", {
            **_event_base(event),
            "kind": "worker",
            "attempts_run": _count(payload.get("attempts_run")),
            "exploit_confirmed": payload.get("exploit_confirmed") is True,
            "original_reward": _number(payload.get("original_reward")),
            "repaired_reward": _number(payload.get("repaired_reward")),
            "status": _text(event.get("status"), 30),
        }
    if kind == "qa.finding/v1":
        return "qa_findings", _qa_finding(event, payload)
    if kind == "task_quality.blocked":
        return "qa_findings", _qa_blocked(event, payload)
    if kind == "task_quality.repair_qualified":
        return "repairs", _repair(event, payload)
    return None


class JournalTaskIndex:
    """Incremental per-task index over the journal's seeker and Task QA events."""

    def __init__(self, read: Callable[[Sequence[str], int, int], list[dict[str, Any]]]) -> None:
        self._read = read
        self._lock = threading.Lock()
        self._after = 0
        self._by_key: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)

    def _refresh(self) -> None:
        while True:
            events = self._read(JOURNAL_EVENT_TYPES, self._after, _BATCH)
            for event in events:
                sequence = _count(event.get("seq"))
                if sequence is not None:
                    self._after = max(self._after, sequence)
                classified = _classify(event)
                payload = event.get("payload")
                if classified is None or not isinstance(payload, Mapping):
                    continue
                for field in ("task_id", "item_id"):
                    key = payload.get(field)
                    if isinstance(key, str) and key:
                        self._by_key[key].append(classified)
            if len(events) < _BATCH:
                return

    def for_keys(self, keys: Collection[str]) -> dict[str, list[dict[str, Any]]]:
        """Events naming any of ``keys`` (task id or item id), oldest first, each once."""
        with self._lock:
            self._refresh()
            seen: set[tuple[str, int | None, str | None]] = set()
            found: dict[str, list[dict[str, Any]]] = {"seeker": [], "qa_findings": [], "repairs": []}
            for key in keys:
                for section, record in self._by_key.get(key, ()):
                    identity = (section, record["seq"], record["kind"])
                    if identity not in seen:
                        seen.add(identity)
                        found[section].append(record)
        for records in found.values():
            records.sort(key=lambda record: record["seq"] or 0)
        return found


class ToolFindingIndex:
    """The latest result per run, tool and detector, re-read only when the file changes."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._key: tuple[int, int, int] | None = None
        self._loaded = False
        self._latest: dict[str, dict[tuple[str, str], tuple[Any, bool | None, list[int]]]] = {}
        self.error: str | None = None

    def _signature(self) -> tuple[int, int, int] | None:
        try:
            seen = self._path.lstat()
        except OSError:
            return None
        return (seen.st_mtime_ns, seen.st_size, seen.st_ino)

    def _load(self) -> None:
        latest: dict[str, dict[tuple[str, str], tuple[Any, bool | None, list[int]]]] = {}
        self.error = None
        try:
            for line in iter_lines(self._path):
                if not line.strip():
                    continue
                try:
                    row = strict_loads(line.decode("utf-8"))
                    run, tool, detector = row["run_id"], row["tool"], row["detector"]
                    moment = parse_moment(row["at"])
                    flag = row["flag"]
                    steps = [
                        step for step in row["evidence_steps"] if isinstance(step, int) and step >= 0
                    ][:5]
                except (ValueError, KeyError, TypeError, UnicodeDecodeError):
                    continue
                if not (
                    isinstance(run, str)
                    and isinstance(tool, str)
                    and isinstance(detector, str)
                    and (flag is None or isinstance(flag, bool))
                ):
                    continue
                kept = latest.setdefault(run, {})
                if (tool, detector) not in kept or moment > kept[(tool, detector)][0]:
                    kept[(tool, detector)] = (moment, flag, steps)
        except FileNotFoundError:
            self.error = "not retained"
        except (OSError, ValueError) as error:
            self.error = (str(error).splitlines() or [type(error).__name__])[0][:200]
        self._latest = latest

    def _current(self) -> dict[str, dict[tuple[str, str], tuple[Any, bool | None, list[int]]]]:
        with self._lock:
            key = self._signature()
            if not self._loaded or key != self._key:
                self._key, self._loaded = key, True
                self._load()
            return self._latest

    def partition(self, tool: str, admitted: Collection[str]) -> dict[str, Any]:
        """Admitted runs split by what one tool recorded: hit, read clean, or not read.

        A run is a hit if any of the tool's detectors flagged it, read clean if one read it
        and none flagged, and not read if the tool has no result or only abstained. Runs the
        tool read that are not in ``admitted`` are only counted, never listed.
        """
        latest = self._current()
        admitted_set = set(admitted)
        hit: dict[str, list[dict[str, Any]]] = {}
        clean: list[str] = []
        unread: list[str] = []
        for run in sorted(admitted_set):
            flagged: list[dict[str, Any]] = []
            read_clean = False
            for (name, detector), (_, flag, steps) in sorted(latest.get(run, {}).items()):
                if name != tool:
                    continue
                if flag is True:
                    flagged.append({"detector": detector, "evidence_steps": steps})
                elif flag is False:
                    read_clean = True
            if flagged:
                hit[run] = flagged
            elif read_clean:
                clean.append(run)
            else:
                unread.append(run)
        outside = sum(
            1
            for run, kept in latest.items()
            if run not in admitted_set and any(name == tool for name, _ in kept)
        )
        return {"hit": hit, "clean": clean, "unread": unread, "outside_n": outside}

    def for_runs(self, run_ids: Collection[str]) -> dict[str, dict[str, dict[str, Any]]]:
        """Per run and tool: the detectors that flagged it and how many read clean or abstained."""
        latest = self._current()
        found: dict[str, dict[str, dict[str, Any]]] = {}
        for run in run_ids:
            tools: dict[str, dict[str, Any]] = {}
            for (tool, detector), (_, flag, steps) in sorted(latest.get(run, {}).items()):
                entry = tools.setdefault(tool, {"flagged": [], "clean_n": 0, "abstained_n": 0})
                if flag is True:
                    entry["flagged"].append({"detector": detector, "evidence_steps": steps})
                elif flag is False:
                    entry["clean_n"] += 1
                else:
                    entry["abstained_n"] += 1
            if tools:
                found[run] = tools
        return found


def _slug(task_id: str) -> str:
    return _SLUG.sub("-", task_id).strip("-")[:80] or "task"


def export_command(task_id: str, run_ids: Sequence[str]) -> str:
    """Blinded ATIF export of the named runs (labels stay in the sealed manifest)."""
    slug = _slug(task_id)
    parts = [
        "uv run evallab prod export-runs",
        "--source-database derived/production/cloud-trace-db.sqlite",
        f"--output-directory /private/tmp/evallab-task-evidence/{slug}",
        "--format atif",
        f"--manifest /private/tmp/evallab-task-evidence/{slug}.manifest.json",
        *(f"--trace-id {shlex.quote(run)}" for run in run_ids[:MAX_EXPORT_IDS]),
    ]
    return " \\\n  ".join(parts)


def fetch_commands(task_id: str, run_ids: Sequence[str]) -> list[dict[str, Any]]:
    """Copyable commands that fetch the same evidence; each says what it needs."""
    quoted = shlex.quote(task_id)
    commands = [
        {
            "label": "This page as JSON",
            "command": (
                'curl -sS -H "Authorization: Bearer $EVALLAB_CLOUD_TOKEN" '
                f'"$EVALLAB_CLOUD_URL/api/cloud-db/task-evidence?split=dev&task_id={quote(task_id, safe="")}"'
            ),
            "needs": "EVALLAB_CLOUD_URL and EVALLAB_CLOUD_TOKEN for the cloud deployment",
        },
        {
            "label": "Local task dossier",
            "command": f"uv run evallab task {quoted} --json",
            "needs": "a local derived/ root; missing evidence is unknown, not clean",
        },
    ]
    if run_ids:
        shown = min(len(run_ids), MAX_EXPORT_IDS)
        commands.append(
            {
                "label": f"Blinded ATIF export of {shown} run{'s' if shown != 1 else ''}",
                "command": export_command(task_id, run_ids),
                "needs": "the local cloud-trace-db.sqlite copy; refuses runs the registry does not admit",
            }
        )
    return commands



def build_task_evidence(
    *,
    task: Mapping[str, Any],
    issues: Sequence[Mapping[str, Any]],
    traces: Mapping[str, Mapping[str, Any]],
    facets: Mapping[str, Mapping[str, Any]],
    cluster_paths: Callable[[str], list[list[dict[str, str]]]],
    tools: Mapping[str, Mapping[str, Mapping[str, Any]]],
    tools_error: str | None,
    journal: Mapping[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Join the already-admitted pieces of one task into the response document."""
    metadata = task.get("metadata_json")
    item_id = metadata.get("item_id") if isinstance(metadata, Mapping) else None
    ordered = sorted(
        traces.values(),
        key=lambda trace: (-(facets.get(trace["trace_id"]) or {}).get("suspicion", -1), trace["trace_id"]),
    )
    runs: list[dict[str, Any]] = []
    for trace in ordered[:MAX_RUNS]:
        run_id = trace["trace_id"]
        facet = facets.get(run_id)
        runs.append(
            {
                "run_id": run_id,
                "model_id": trace.get("model_id"),
                "step_count": trace["step_count"],
                "trust_tier": trace["trust_tier"],
                "outcome": trace["outcome"],
                "facet": None
                if facet is None
                else {
                    key: facet[key]
                    for key in (
                        "goal",
                        "approach",
                        "shortcut",
                        "suspicion",
                        "tests",
                        "eval_awareness",
                        "grader_or_harness_touched",
                        "answer_material_accessed",
                        "success_claim_without_evidence",
                        "evidence_steps",
                        "facet_model",
                    )
                },
                "clusters": [
                    [cluster["name"] for cluster in path] for path in cluster_paths(run_id)
                ]
                if facet is not None
                else [],
                "tools": tools.get(run_id, {}),
                "export_command": export_command(task["task_id"], [run_id]),
            }
        )
    flagged = sum(
        1 for run in runs if any(entry["flagged"] for entry in run["tools"].values())
    )
    seeker = journal["seeker"]
    summary = {
        "runs_n": len(traces),
        "runs_shown_n": len(runs),
        "faceted_n": sum(1 for trace in traces if trace in facets),
        "suspicion_at_least_2_n": sum(
            1 for facet in facets.values() if facet.get("suspicion", 0) >= 2
        ),
        "tool_flagged_runs_n": flagged,
        "issues_n": len(issues),
        "issues_confirmed_n": sum(1 for issue in issues if issue.get("status") == "confirmed"),
        "qa_findings_n": sum(1 for record in journal["qa_findings"] if record["kind"] == "finding"),
        "seeker_attempts_n": sum(1 for record in seeker if record["kind"] == "attempt"),
        "seeker_confirmed_n": sum(1 for record in seeker if record["kind"] == "confirmed"),
        "repairs_qualified_n": sum(1 for record in journal["repairs"] if record["qualified"]),
    }
    return {
        "schema": SCHEMA,
        "task": {
            "task_id": task["task_id"],
            "dataset_id": task["dataset_id"],
            "item_id": item_id if isinstance(item_id, str) else None,
            "split": task["split"],
            "original_grader_digest": task.get("original_grader_digest"),
            "repaired_grader_digest": task.get("repaired_grader_digest"),
        },
        "summary": summary,
        "runs": runs,
        "issues": [
            {
                key: issue.get(key)
                for key in (
                    "issue_id",
                    "defect_class",
                    "direction",
                    "status",
                    "trust_tier",
                    "original_reward",
                    "repaired_reward",
                    "citation_file",
                    "citation_line",
                    "witness_digest",
                )
            }
            for issue in issues
        ],
        "qa_findings": journal["qa_findings"],
        "seeker": seeker,
        "repairs": journal["repairs"],
        "commands": fetch_commands(task["task_id"], [run["run_id"] for run in runs]),
        "tool_results_error": tools_error,
        "notice": (
            "Retained records for one admitted DEV task, not a verdict. Atlas facets are "
            "model-read leads; a tool that did not read a run abstained, which is neither a "
            "miss nor a clean pass; no finding recorded means none recorded. Exploit files, "
            "instruction text and reproducer scripts are never returned. Fetch commands "
            "export blinded copies; labels stay in a sealed manifest."
        ),
    }
