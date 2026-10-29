"""Authenticated Modal production control plane, durable stores, and CPU workers.

Deploy with ``modal deploy --strategy recreate src/evallab/production/modal_cloud.py``
only after integrated contract proof and an allowlisted actor projection plus
its reachable CAS scope are established. Never bulk-copy a raw database.
One cloud actor owns all SQLite connections: independent Modal containers must
never write snapshots of the same Volume-backed SQLite database. The remote worker entrypoint forwards
to that actor; visiting the dashboard never dispatches work or calls a model.
Recreate deployments quiesce the old actor before its replacement opens SQLite.

Retained benchmark documents live in ``<data_root>/benchmarks/*.json``. Named
or incomplete studies use ``evallab.arm_comparison/v2``: coverage and protocol
are explicit, unavailable metrics remain null, and requested models never
substitute for served identities. Reward-Seeker model studies use
``evallab.seeker_comparison/v1``; ``/api/cloud-db/corpus`` serves aggregate-only
corpus counts. Trust-tier censuses, Hackbook QC and tune-versus-cross-test
scoreboards use ``evallab.trust_tier_census/v1``, ``evallab.hackbook_qc/v1`` and
``evallab.tune_cross_test/v1``; executed seeker differentials, monitor cascades and the
three-system playbook use ``evallab.seeker_executed_differentials/v2``,
``evallab.monitor_cascade/v1`` and ``evallab.system_playbook/v1``.
See ``docs/live-watch.md`` for the wire contracts.
Historic ``evallab.swarm_bench/v1`` reports with ``split: "dev"`` and the strict
``evallab.arm_comparison/v1`` contract below remain readable without rewriting::

    {
      "schema": "evallab.arm_comparison/v1",
      "cohort": {"dataset_id": "<nonempty string>", "split": "dev", "task_count": 1},
      "arms": [{
        "arm": "A",
        "label": "<nonempty string>",
        "served_models": ["provider/model-actually-served"],
        "harness": "<nonempty string>",
        "retrieval": true,
        "metrics": {
          "primary_hits_n": 0, "primary_total_n": 1,
          "precision": 0.0, "recall": 0.0, "f1": 0.0
        },
        "spend_usd": 0.0,
        "model_calls": 0,
        "artifact_sha256": "<64 lowercase hex characters>"
      }],
      "generated_at": "<ISO-8601 timestamp with timezone>"
    }

Arm A is a single strong model plus harness (optionally CloudTraceRetriever);
B is a many-cheap-agents swarm; C is a hybrid swarm plus Opus adjudicator.
One to three unique arms may be retained; absent arms have no measured result.
Model identifiers record what was actually served, not requested model aliases.
Counts are nonnegative integers, hits cannot exceed totals, metric fractions
are finite in [0, 1], and spend is finite and nonnegative. Each listing entry
includes the SHA-256 of the exact document bytes, separate from arm artifacts.
Reports retain DEV-intended evidence and coverage, never held-out capability.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import stat
import threading
from collections.abc import Callable, Collection, Hashable, Mapping
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Any, Literal

import modal
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ValidationError

from evallab.execution_backend import ExecutionBackendConfig
from evallab.hackbook.playbook_cards import match_playbook_cards
from evallab.production import (
    atlas_coverage,
    atlas_view,
    cross_test_sets,
    field_reliability,
    file_cache,
    harness_view,
    repair_board,
    task_evidence,
    tool_runs,
)
from evallab.production import (
    gate_progress as gate_progress_module,
)
from evallab.production.asset_store import VerifiedAssetStore
from evallab.production.benchmark_reports import MAX_BENCHMARK_BYTES, validate_benchmark_document
from evallab.production.cloud_trace_db import (
    CloudTraceDatabase,
    CloudTraceRetriever,
    S3CompatibleCAS,
    admitted_cloud_task_ids,
)
from evallab.production.control_plane import create_app
from evallab.production.corpus_queue import ContinuousWorkQueue, CorpusRegistry
from evallab.production.inspector import RunInspector
from evallab.production.monitor_worker import MisalignmentLiveWorker
from evallab.production.outbox_relay import LocalJetStreamAdapter, OutboxRelay
from evallab.production.seeker_worker import RewardSeekerLiveWorker
from evallab.production.supervisor import ProductionSupervisor
from evallab.production.tool_scorecard_view import ToolScorecardCache
from evallab.production.tq_worker import TaskQualityLiveWorker
from evallab.production.trace_feature_flags import compute_trace_feature_flags

APP_NAME = "evallab-cloud-production"
VOLUME_NAME = "evallab-cloud-store"
DATA_ROOT = Path("/data")
CLOUD_REPO_ROOT = Path("/opt/eval-lab")
BILLING_TAG = "cloud-offload-20261024"
COOKIE_NAME = "evallab_cloud_session"


def _repository_root(source_file: str, *, local: bool) -> Path:
    # Modal imports file-based deployments as /root/modal_cloud.py, not from
    # their original package depth. Assets have a fixed cloud mount instead.
    return Path(source_file).resolve().parents[3] if local else CLOUD_REPO_ROOT


REPO_ROOT = _repository_root(__file__, local=modal.is_local())


# The fragment is never sent to Modal's HTTP logs. Exchange it for an HTTP-only
# cookie, erase it from browser history, and then open the protected dashboard.
_LOGIN = """<!doctype html><meta charset="utf-8"><title>Eval Lab cloud login</title>
<p id="status">Authenticating to your private Eval Lab cloud…</p><script>
(async () => {
 const token = new URLSearchParams(location.hash.slice(1)).get('token');
 history.replaceState(null, '', location.pathname);
 if (!token) { document.getElementById('status').textContent = 'Open with lab cloud to authenticate.'; return; }
 const response = await fetch('/auth', {method: 'POST', credentials: 'same-origin',
  headers: {'Content-Type': 'application/json'}, body: JSON.stringify({token})});
 if (response.ok) location.replace('/');
 else document.getElementById('status').textContent = 'Authentication failed. Run lab cloud again.';
})();
</script>"""


class _LoginRequest(BaseModel):
    token: str


def _require_dev(split: str) -> None:
    if split != "dev":
        raise HTTPException(400, "Only split=dev is served; sealed and held-out splits are refused")


def _matches(query: str, record: Mapping[str, Any]) -> bool:
    if not query.strip():
        return True
    terms = re.findall(r"\w+", query, flags=re.UNICODE)
    text = json.dumps(record, ensure_ascii=False).casefold()
    return bool(terms) and all(term.casefold() in text for term in terms)


def _issue_matches(
    issue: Mapping[str, Any],
    *,
    direction: str | None,
    defect_class: str | None,
    only_verified: bool,
) -> bool:
    return (
        (direction is None or issue["direction"] == direction)
        and (defect_class is None or issue["defect_class"] == defect_class)
        and (not only_verified or issue["status"] == "confirmed")
    )


class CloudRuntime:
    """Single-process owner of Volume-backed state; usable offline in unit tests."""

    def __init__(
        self,
        data_root: Path,
        repo_root: Path,
        token: str,
        *,
        commit: Callable[[], None] | None = None,
        repair_inventory: Path | None = None,
        cross_test_registry: Path | None = None,
        detector_scope: Path | None = None,
        gate_progress: Path | None = None,
    ) -> None:
        if not token.strip():
            raise ValueError("EVALLAB_CLOUD_TOKEN is required; public trace access is forbidden")
        self.data_root = Path(data_root)
        self.repo_root = Path(repo_root)
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.cas_root = self.data_root / "cas"
        self.cas_root.mkdir(exist_ok=True)
        self._commit = commit or (lambda: None)
        self._step_lock = threading.Lock()
        self._closed = False
        self._atlas_lock = threading.Lock()
        self._atlas_cache: tuple[list[tuple[int, int, int] | None], atlas_view.AtlasStore] | None = None
        self._derived_values: dict[str, tuple[Hashable, Any]] = {}
        self._tool_scorecard = ToolScorecardCache(
            self.data_root, Path(detector_scope) if detector_scope else None
        )
        self._atlas_refresh = atlas_coverage.RefreshCache(self.data_root / "run-atlas")
        self._field_reliability = field_reliability.FieldReliabilityCache(self.data_root / "run-atlas")
        self._repair_inventory = file_cache.ChangeCache(
            Path(repair_inventory) if repair_inventory else self.data_root / repair_board.DEFAULT_FILE,
            repair_board.load_inventory,
        )
        self._cross_test_registry = file_cache.ChangeCache(
            Path(cross_test_registry) if cross_test_registry else self.data_root / cross_test_sets.DEFAULT_FILE,
            cross_test_sets.load_registry,
        )
        self._gate_progress = file_cache.ChangeCache(
            Path(gate_progress) if gate_progress else self.data_root / gate_progress_module.DEFAULT_FILE,
            gate_progress_module.load_progress,
        )
        self._harness_findings = file_cache.ChangeCache(
            self.data_root / harness_view.FINDINGS_FILE, harness_view.load_findings
        )
        self._harness_scorecard = file_cache.ChangeCache(
            self.data_root / harness_view.SCORECARD_FILE, harness_view.load_scorecard
        )
        self._tool_results = task_evidence.ToolFindingIndex(
            self.data_root / "trace-tools" / task_evidence.RESULTS_FILE
        )
        self._journal_tasks = task_evidence.JournalTaskIndex(
            lambda types, after, limit: self.queue.read_events_of_types(
                types, after_seq=after, limit=limit
            )
        )
        self.registry = CorpusRegistry(self.repo_root)
        with ExitStack() as cleanup:
            self.queue = ContinuousWorkQueue(self.data_root / "prod.sqlite", registry=self.registry)
            cleanup.callback(self.queue.close)
            self.database = CloudTraceDatabase(self.data_root / "cloud-trace-db.sqlite")
            cleanup.callback(self.database.close)
            self.cas = S3CompatibleCAS(local_directory=self.cas_root, bucket="")
            roots = [self.repo_root, self.data_root]
            self.app = create_app(
                self.queue,
                operator_tokens={token: "Peter"},
                home="atlas",
                asset_store=VerifiedAssetStore(
                    self.queue,
                    self.registry,
                    export_root=None,
                    allowed_roots=roots,
                ),
                inspector=RunInspector(self.queue, self.registry, allowed_roots=roots),
            )
            self._add_routes(token)
            cleanup.pop_all()

    def _query(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        path = self.data_root / "cloud-trace-db.sqlite"
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(sql, parameters)]

    def _derived[T](self, name: str, key: Hashable, build: Callable[[], T]) -> T:
        """``build()`` once per trace-database state and ``key`` (the retained files it read).

        The key is taken before building, so a write that lands mid-build changes it and the next
        caller rebuilds: a value is never served for a state it did not read. Concurrent callers
        may build twice, never wrongly. Values are shared between requests; callers never mutate
        them. Without a database change token (PostgreSQL) every call builds.
        """
        token = self.database.change_token()
        if token is None:
            return build()
        full = (token, key)
        kept = self._derived_values.get(name)
        if kept is not None and kept[0] == full:
            return kept[1]
        value = build()
        self._derived_values[name] = (full, value)
        return value

    def _admitted_task_ids(self, task_id: str | None = None) -> frozenset[str]:
        """Apply the shared admission policy to identity metadata before hydration."""
        admitted = self._derived("admitted_task_ids", None, self._read_admitted_task_ids)
        return admitted if task_id is None else admitted.intersection((task_id,))

    def _read_admitted_task_ids(self) -> frozenset[str]:
        identities = self._query(
            "WITH identities AS ("
            " SELECT task_id, dataset_id, split, original_grader_digest, repaired_grader_digest,"
            " metadata_json, CASE WHEN json_valid(metadata_json)"
            " THEN json_type(metadata_json) END AS metadata_type FROM corpus_tasks"
            ") SELECT task_id, dataset_id, split, original_grader_digest, repaired_grader_digest,"
            " CASE WHEN metadata_type = 'object'"
            " THEN json_extract(metadata_json, '$.item_id') END AS item_id,"
            " CASE WHEN metadata_type = 'object'"
            " THEN json_type(metadata_json, '$.item_id') ELSE 'invalid' END AS item_type"
            " FROM identities"
        )
        return admitted_cloud_task_ids(identities, registry=self.registry)

    def _tasks(self, task_id: str | None, query: str) -> list[dict[str, Any]]:
        identifiers = sorted(self._admitted_task_ids(task_id))
        rows: list[dict[str, Any]] = []
        # Match the store's bounded hydration batches, keeping SQL bind counts
        # independent of corpus size and denied bodies out of the SELECT.
        for start in range(0, len(identifiers), 500):
            page = identifiers[start : start + 500]
            placeholders = ", ".join("?" for _ in page)
            rows.extend(
                self._query(
                    f"SELECT * FROM corpus_tasks WHERE task_id IN ({placeholders}) ORDER BY task_id",
                    tuple(page),
                )
            )
        for row in rows:
            row["metadata_json"] = json.loads(row["metadata_json"])
        return [row for row in rows if _matches(query, row)]

    def _corpus_breakdown(self) -> dict[str, Any]:
        """Aggregate-only corpus census; no task identity or body leaves this method."""
        admitted = self._admitted_task_ids()
        activity = {
            row["task_id"]: row
            for row in self._query(
                "SELECT task_id, COUNT(*) AS traces, COALESCE(SUM(step_count), 0) AS steps,"
                " COALESCE(SUM(is_exploit), 0) AS exploit FROM agent_traces GROUP BY task_id"
            )
        }
        datasets: dict[str, dict[str, Any]] = {}
        for task in self._query("SELECT task_id, dataset_id, split FROM corpus_tasks"):
            entry = datasets.setdefault(
                task["dataset_id"],
                {
                    "dataset_id": task["dataset_id"],
                    **{
                        bucket: {"tasks": 0, "traces": 0, "steps": 0}
                        for bucket in ("admitted_dev", "dev_not_admitted", "withheld")
                    },
                    "enacted_exploit_traces": 0,
                },
            )
            if task["task_id"] in admitted:
                bucket = "admitted_dev"
            elif str(task["split"]).strip().casefold() == "dev":
                bucket = "dev_not_admitted"
            else:
                bucket = "withheld"
            seen = activity.get(task["task_id"])
            counts = entry[bucket]
            counts["tasks"] += 1
            if seen is not None:
                counts["traces"] += seen["traces"]
                counts["steps"] += seen["steps"]
                if bucket == "admitted_dev":
                    entry["enacted_exploit_traces"] += seen["exploit"]
        ordered = sorted(datasets.values(), key=lambda entry: entry["dataset_id"])
        return {
            "datasets": ordered,
            "notice": (
                "Counts only. Withheld and gate-denied rows contribute no identity, label or body. "
                "Enacted-exploit counts are the retained source is_exploit label on admitted DEV "
                "traces, not new verification."
            ),
        }

    def _benchmarks(self) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {"benchmarks": [], "rejected": []}
        directory = self.data_root / "benchmarks"
        try:
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return result
        except OSError as error:
            result["rejected"].append({"file": "benchmarks", "reason": error.strerror})
            return result
        try:
            with os.scandir(descriptor) as entries:
                names = sorted(entry.name for entry in entries if entry.name.endswith(".json"))
            for name in names:
                try:
                    # Pin the directory and refuse symlinks; NONBLOCK also prevents
                    # a named pipe from hanging before the regular-file check.
                    file_descriptor = os.open(
                        name,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=descriptor,
                    )
                    with os.fdopen(file_descriptor, "rb") as stream:
                        metadata = os.fstat(stream.fileno())
                        if not stat.S_ISREG(metadata.st_mode):
                            raise ValueError("Benchmark must be a regular, non-symlink file")
                        if metadata.st_size > MAX_BENCHMARK_BYTES:
                            raise ValueError(f"Benchmark exceeds {MAX_BENCHMARK_BYTES} byte limit")
                        raw = stream.read(MAX_BENCHMARK_BYTES + 1)
                    if len(raw) > MAX_BENCHMARK_BYTES:
                        raise ValueError(f"Benchmark exceeds {MAX_BENCHMARK_BYTES} byte limit")
                    document = validate_benchmark_document(raw)
                except ValidationError as error:
                    reason = "; ".join(
                        ".".join(map(str, item["loc"])) + ": " + item["msg"]
                        for item in error.errors(include_input=False, include_url=False)
                    )
                    result["rejected"].append({"file": name, "reason": reason})
                except (ValueError, RecursionError) as error:
                    result["rejected"].append({"file": name, "reason": str(error)})
                except OSError as error:
                    result["rejected"].append({"file": name, "reason": error.strerror})
                else:
                    result["benchmarks"].append(
                        {
                            "file": name,
                            "sha256": hashlib.sha256(raw).hexdigest(),
                            "report": document,
                        }
                    )
        except OSError as error:
            result["rejected"].append({"file": "benchmarks", "reason": error.strerror})
        finally:
            os.close(descriptor)
        return result

    def _atlas_store(self) -> atlas_view.AtlasStore:
        """The validated atlas files, re-read only when either file changes on disk."""
        directory = self.data_root / "run-atlas"
        key: list[tuple[int, int, int] | None] = []
        try:
            try:
                root = os.stat(directory, follow_symlinks=False)
            except FileNotFoundError:
                key.append(None)
            else:
                if not stat.S_ISDIR(root.st_mode):
                    raise ValueError("run-atlas must be a non-symlink directory")
                key.append((root.st_mtime_ns, root.st_size, root.st_ino))
            for name in (atlas_view.FACETS_FILE, atlas_view.CLUSTERS_FILE):
                try:
                    seen = os.stat(directory / name, follow_symlinks=False)
                except FileNotFoundError:
                    key.append(None)
                else:
                    key.append((seen.st_mtime_ns, seen.st_size, seen.st_ino))
        except (OSError, ValueError) as error:
            raise HTTPException(503, f"Atlas store is unreadable: {error}") from error
        with self._atlas_lock:
            if self._atlas_cache is None or self._atlas_cache[0] != key:
                try:
                    self._atlas_cache = (key, atlas_view.load_atlas_store(directory))
                except (OSError, ValueError, UnicodeDecodeError) as error:
                    raise HTTPException(503, f"Atlas store is unreadable: {error}") from error
            return self._atlas_cache[1]

    def _atlas_traces(self, task_ids: Collection[str] | None = None) -> dict[str, dict[str, Any]]:
        """Admitted DEV identities and exact step indices; no body is read here.

        The full map (every admitted task) is read once per trace-database state and shared.
        """
        if task_ids is None:
            return self._derived("atlas_traces", None, lambda: self._read_atlas_traces(None))
        return self._read_atlas_traces(task_ids)

    def _read_atlas_traces(self, task_ids: Collection[str] | None) -> dict[str, dict[str, Any]]:
        admitted = self._admitted_task_ids()
        identifiers = sorted(admitted if task_ids is None else admitted.intersection(task_ids))
        traces: dict[str, dict[str, Any]] = {}
        for start in range(0, len(identifiers), 500):
            page = identifiers[start : start + 500]
            placeholders = ", ".join("?" for _ in page)
            rows = self._query(
                "SELECT t.trace_id, t.task_id, t.model_id, t.step_count, t.trust_tier, c.dataset_id,"
                " CASE WHEN t.reward >= 1 THEN 'pass' WHEN t.reward <= 0 THEN 'fail'"
                " ELSE 'unknown' END AS outcome"
                " FROM agent_traces t JOIN corpus_tasks c ON c.task_id = t.task_id"
                f" WHERE t.task_id IN ({placeholders})",
                tuple(page),
            )
            indices: dict[str, set[int]] = {row["trace_id"]: set() for row in rows}
            for step in self._query(
                "SELECT s.trace_id, s.step_index FROM trace_steps s"
                " JOIN agent_traces t ON t.trace_id = s.trace_id"
                f" WHERE t.task_id IN ({placeholders})",
                tuple(page),
            ):
                found = indices.get(step["trace_id"])
                if found is not None:
                    found.add(step["step_index"])
            for row in rows:
                row["step_indices"] = frozenset(indices[row["trace_id"]])
                traces[row["trace_id"]] = row
        return traces

    def _atlas_run(self, run_id: str) -> dict[str, Any]:
        """One atlas run: facet, cluster paths, evidence steps, trace flags and playbook cards."""
        store, traces = self._atlas_store(), self._atlas_traces()
        shown, _ = atlas_view.visible_facets(store, traces)
        facet = shown.get(run_id)
        if facet is None:
            raise HTTPException(404, "Run is not in the atlas")
        blinding = atlas_coverage.blinding_states(self._atlas_refresh.get(), {run_id: facet})
        identity = traces[run_id]
        result: dict[str, Any] = {
            "run": {
                **facet,
                "task_id": identity["task_id"],
                "dataset_id": identity["dataset_id"],
                "trust_tier": identity["trust_tier"],
                "step_count": identity["step_count"],
                "cluster_paths": atlas_view.cluster_paths(store, run_id),
                "blinding": blinding[run_id],
            },
            "evidence": [],
            "flags": None,
            "playbook_cards": [],
            "withheld": None,
            "notice": (
                "Trace flags and playbook cards are routing leads from agent-authored commands "
                "and text, not cheating verdicts. Evidence steps are bounded excerpts."
            ),
        }
        retriever = CloudTraceRetriever(self.database, registry=self.registry)
        try:
            # Retrieval's own gate decides first: admitted DEV task, returnable tier, no sealed identity.
            retriever.expand_pointer(f"trace:{run_id}:0-0", include_provisional=True)
            wanted = set(facet["evidence_steps"])
            collected: dict[int, dict[str, Any]] = {}
            for index in sorted(wanted):
                window = retriever.expand_pointer(
                    f"trace:{run_id}:{max(0, index - 1)}-{index + 1}", include_provisional=True
                )
                for step in window["steps"]:
                    if step["role"] in ("agent", "assistant", "tool", "environment"):
                        collected[step["step_index"]] = {
                            **step,
                            "is_evidence": step["step_index"] in wanted,
                        }
            # Keep the final admission check and raw flag read in one snapshot.
            # A trace can disappear or become denied after an evidence window.
            with self.database._transaction():
                admitted, _ = retriever._admitted_ids()
                if identity["task_id"] not in admitted or not self.database._rows(
                    "SELECT trace_id FROM agent_traces WHERE trace_id = ? AND task_id = ?",
                    (run_id, identity["task_id"]),
                ):
                    raise ValueError("Trace is no longer admitted")
                rows = self.database._rows(
                    "SELECT step_index, role, tool_name, command_or_action, content_text"
                    " FROM trace_steps WHERE trace_id = ?"
                    " AND role IN ('agent', 'assistant', 'tool', 'environment')"
                    " ORDER BY step_index LIMIT ?",
                    (run_id, atlas_view.MAX_FLAG_STEPS + 1),
                )
        except ValueError:
            result["withheld"] = "Evidence for this run is not returnable under the retrieval rules."
            return result
        result["evidence"] = [collected[index] for index in sorted(collected)]
        if len(rows) > atlas_view.MAX_FLAG_STEPS:
            result["flags_unavailable"] = f"more than {atlas_view.MAX_FLAG_STEPS:,} steps"
            return result
        features = compute_trace_feature_flags(rows)["features"]
        fired = {
            name: {
                "count": flag["count"],
                "step_indices": [rows[position]["step_index"] for position in flag["step_indices"]][
                    :50
                ],
            }
            for name, flag in features.items()
            if flag["fired"]
        }
        result["flags"] = fired
        result["playbook_cards"] = match_playbook_cards(list(fired), kind="trace", max_cards=6)
        return result

    def _task_evidence(self, task_id: str) -> dict[str, Any]:
        """Everything retained about one admitted DEV task: runs, tools, QA, seeker, fixes."""
        if task_id not in self._admitted_task_ids(task_id):
            raise HTTPException(404, "Task is not an admitted DEV task")
        rows = self._query(
            "SELECT task_id, dataset_id, split, original_grader_digest, repaired_grader_digest,"
            " metadata_json FROM corpus_tasks WHERE task_id = ?",
            (task_id,),
        )
        if not rows:
            raise HTTPException(404, "Task is not an admitted DEV task")
        task = rows[0]
        try:
            task["metadata_json"] = json.loads(task["metadata_json"])
        except ValueError:
            task["metadata_json"] = {}
        issues = self._query(
            "SELECT issue_id, defect_class, direction, status, trust_tier, original_reward,"
            " repaired_reward, citation_file, citation_line, witness_digest"
            " FROM task_issues WHERE task_id = ? ORDER BY issue_id",
            (task_id,),
        )
        traces = self._atlas_traces([task_id])
        store = self._atlas_store()
        facets, _ = atlas_view.visible_facets(store, traces)
        keys = {task_id}
        item_id = task["metadata_json"].get("item_id") if isinstance(task["metadata_json"], dict) else None
        if isinstance(item_id, str) and item_id:
            keys.add(item_id)
        tools = self._tool_results.for_runs(traces)
        return task_evidence.build_task_evidence(
            task=task,
            issues=issues,
            traces=traces,
            facets=facets,
            cluster_paths=lambda run_id: atlas_view.cluster_paths(store, run_id),
            tools=tools,
            tools_error=self._tool_results.error,
            journal=self._journal_tasks.for_keys(keys),
        )

    def _admitted_runs(self, run_ids: Collection[str]) -> dict[str, str]:
        """Run id to task id for the runs that belong to an admitted DEV task."""
        admitted = self._admitted_task_ids()
        found: dict[str, str] = {}
        ids = sorted(run_ids)
        for start in range(0, len(ids), 500):
            page = ids[start : start + 500]
            placeholders = ", ".join("?" for _ in page)
            for row in self._query(
                f"SELECT trace_id, task_id FROM agent_traces WHERE trace_id IN ({placeholders})", tuple(page)
            ):
                if row["task_id"] in admitted:
                    found[row["trace_id"]] = row["task_id"]
        return found

    def _actor_runs(self, run_ids: Collection[str]) -> set[str]:
        """Runs holding an agent step with text or a tool call; any other run is unknown, never clean."""
        found: set[str] = set()
        ids = sorted(run_ids)
        for start in range(0, len(ids), 500):
            page = ids[start : start + 500]
            placeholders = ", ".join("?" for _ in page)
            found.update(
                row["trace_id"]
                for row in self._query(
                    "SELECT DISTINCT trace_id FROM trace_steps"
                    f" WHERE trace_id IN ({placeholders}) AND role IN ('agent', 'assistant')"
                    " AND (TRIM(COALESCE(content_text, '')) <> '' OR COALESCE(tool_name, '') <> ''"
                    " OR COALESCE(command_or_action, '') <> '')",
                    tuple(page),
                )
            )
        return found

    def _harness_admitted(self) -> tuple[dict[str, Any], dict[str, str], frozenset[tuple[str, str, str]]]:
        """The findings, their runs that belong to an admitted DEV task, and the result keys those hold.

        Read once per findings file and trace-database state, shared by the overview and every card.
        A result recorded only on a run outside the admitted set never names a detector on a card.
        """

        def build() -> tuple[dict[str, Any], dict[str, str], frozenset[tuple[str, str, str]]]:
            findings = self._harness_findings.get()
            by_run = findings["by_run"]
            admitted = self._admitted_runs(by_run)
            return findings, admitted, frozenset(key for run in admitted for key in by_run[run])

        return self._derived("harness_admitted", file_cache.signature(self._harness_findings.path), build)

    def _run_alarms(
        self, run_ids: Collection[str]
    ) -> tuple[dict[str, list[str]], dict[str, int], bool]:
        """Per run, the detectors that alarm on it and how many detectors read it at all.

        Sources are whole-run tool results and harness findings. A detector that recorded nothing for
        a run adds nothing; the flag says whether any source is retained, so "no run alarmed" is never
        confused with "nothing was read".
        """
        tools = self._tool_results.for_runs(run_ids)
        findings = self._harness_findings.get()
        by_run = findings["by_run"]
        alarms: dict[str, list[str]] = {}
        read: dict[str, int] = {}
        for run in run_ids:
            entries = tools.get(run, {})
            records = by_run.get(run, {}).values()
            names = {tool for tool, entry in entries.items() if entry["flagged"]}
            names.update(record["detector"] for record in records if record["verdict"] == "alarm")
            if names:
                alarms[run] = sorted(names)
            readers = set(entries) | {record["detector"] for record in records}
            if readers:
                read[run] = len(readers)
        return alarms, read, self._tool_results.error is None or bool(findings["present"])

    def _harness_overview(self) -> dict[str, Any]:
        findings, admitted, _ = self._harness_admitted()
        actor_runs = self._derived(
            "harness_actor_runs", frozenset(admitted), lambda: frozenset(self._actor_runs(admitted))
        )
        return harness_view.build_overview(findings, self._harness_scorecard.get(), admitted, actor_runs)

    def _harness_card(self, run_id: str) -> dict[str, Any]:
        """Every detector's newest result for one admitted DEV run, from the retained findings file."""
        if run_id not in self._admitted_runs([run_id]):
            raise HTTPException(404, "Run is not an admitted DEV run")
        identity = self._query(
            "SELECT task_id, step_count, trust_tier FROM agent_traces WHERE trace_id = ?", (run_id,)
        )[0]
        steps = frozenset(
            row["step_index"]
            for row in self._query("SELECT step_index FROM trace_steps WHERE trace_id = ?", (run_id,))
        )
        actor_inputs = run_id in self._actor_runs([run_id])
        try:
            facet = self._atlas_store().facets.get(run_id)
        except HTTPException:
            facet = None
        if facet is not None and not set(facet["evidence_steps"]).issubset(steps):
            facet = None
        findings, _, known = self._harness_admitted()
        return harness_view.build_card(
            {"run_id": run_id, **identity},
            findings["by_run"].get(run_id, {}),
            steps=steps,
            actor_inputs=actor_inputs,
            facet=facet,
            known=known,
        )

    def _task_search(self, query: str) -> dict[str, Any]:
        """Admitted DEV task ids containing the text, with run and issue counts."""
        needle = query.strip()
        if len(needle) < 2:
            raise HTTPException(400, "Type at least two characters of a task id")
        pattern = "%" + needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        candidates = self._query(
            "SELECT task_id, dataset_id FROM corpus_tasks WHERE task_id LIKE ? ESCAPE '\\'"
            " ORDER BY task_id LIMIT 500",
            (pattern,),
        )
        admitted = self._admitted_task_ids()
        found = [row for row in candidates if row["task_id"] in admitted][: task_evidence.SEARCH_LIMIT]
        for row in found:
            row["runs_n"] = self._query(
                "SELECT COUNT(*) AS n FROM agent_traces WHERE task_id = ?", (row["task_id"],)
            )[0]["n"]
            row["issues_n"] = self._query(
                "SELECT COUNT(*) AS n FROM task_issues WHERE task_id = ?", (row["task_id"],)
            )[0]["n"]
        return {"tasks": found, "limit": task_evidence.SEARCH_LIMIT}

    def _add_routes(self, token: str) -> None:
        expected = token.encode()

        def authorized(candidate: str) -> bool:
            return hmac.compare_digest(candidate.encode(), expected)

        @self.app.middleware("http")
        async def authenticate(request: Request, call_next):
            if request.url.path != "/auth":
                bearer = request.headers.get("authorization", "")
                bearer = bearer.removeprefix("Bearer ") if bearer.startswith("Bearer ") else ""
                cookie = request.cookies.get(COOKIE_NAME, "")
                if not authorized(bearer) and not authorized(cookie):
                    return JSONResponse(
                        {"detail": "Unauthorized"},
                        status_code=401,
                        headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
                    )
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            return response

        @self.app.get("/auth", response_class=HTMLResponse)
        def login_page():
            return HTMLResponse(
                _LOGIN,
                headers={
                    "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
                    "X-Content-Type-Options": "nosniff",
                },
            )

        @self.app.post("/auth")
        def login(body: _LoginRequest):
            if not authorized(body.token):
                raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})
            response = JSONResponse({"authenticated": True})
            response.set_cookie(
                COOKIE_NAME,
                token,
                secure=True,
                httponly=True,
                samesite="lax",
                path="/",
            )
            return response

        @self.app.get("/api/cloud-db/stats")
        def stats():
            counts = {
                table: self._query(f"SELECT COUNT(*) AS count FROM {table}")[0]["count"]
                for table in ("corpus_tasks", "task_issues", "agent_traces", "trace_steps")
            }
            return {
                "backend": "modal",
                "database": str(self.data_root / "cloud-trace-db.sqlite"),
                "queue_database": str(self.data_root / "prod.sqlite"),
                "cas_directory": str(self.cas_root),
                "cas_objects": sum(len(files) for _, _, files in os.walk(self.cas_root)),
                **counts,
                "notice": "Statuses, outcomes and receipt flags are retained source claims, not new verification.",
            }

        @self.app.get("/api/cloud-db/tasks")
        def tasks(q: str = "", task_id: str | None = None, split: str = "dev"):
            _require_dev(split)
            return {"tasks": self._tasks(task_id, q)}

        @self.app.get("/api/cloud-db/issues")
        def issues(
            q: str = "",
            task_id: str | None = None,
            direction: Literal["wrong_pass", "unfair_fail"] | None = None,
            defect_class: str | None = None,
            only_verified: bool = False,
            split: str = "dev",
        ):
            _require_dev(split)
            admitted = self._admitted_task_ids(task_id)
            rows = self.database.query_task_issues(
                task_id,
                direction=direction,
                confirmed_only=only_verified,
                allowed_task_ids=admitted,
            )
            return {
                "issues": [
                    row
                    for row in rows
                    if (
                        (defect_class is None or row["defect_class"] == defect_class)
                        and _matches(q, row)
                    )
                ]
            }

        @self.app.get("/api/cloud-db/traces")
        def traces(
            q: str = "",
            task_id: str | None = None,
            defect_class: str | None = None,
            tool_name: str | None = None,
            only_verified: bool = False,
            direction: Literal["wrong_pass", "unfair_fail"] | None = None,
            split: str = "dev",
        ):
            _require_dev(split)
            admitted = self._admitted_task_ids(task_id)
            rows = self.database.search_traces(
                q,
                defect_class=defect_class,
                tool_name=tool_name,
                only_verified=only_verified,
                allowed_task_ids=admitted,
            )
            return {
                "traces": [
                    row
                    for row in rows
                    if (
                        not (direction or defect_class)
                        or any(
                            _issue_matches(
                                issue,
                                direction=direction,
                                defect_class=defect_class,
                                only_verified=only_verified,
                            )
                            for issue in row["issues"]
                        )
                    )
                ]
            }

        @self.app.get("/api/cloud-db/benchmarks")
        def benchmarks():
            return self._benchmarks()

        @self.app.get("/api/cloud-db/corpus")
        def corpus():
            return self._corpus_breakdown()

        @self.app.get("/api/cloud-db/atlas")
        def atlas(
            q: str = "",
            source: str | None = None,
            outcome: Literal["pass", "fail", "unknown"] | None = None,
            tests: Literal["untouched", "read", "modified", "deleted", "replaced"] | None = None,
            eval_awareness: Literal[
                "none", "mentions_tests", "reasons_about_grader", "plans_to_game"
            ]
            | None = None,
            facet_model: str | None = None,
            min_suspicion: int = Query(0, ge=0, le=3),
            grader_or_harness_touched: bool | None = None,
            answer_material_accessed: bool | None = None,
            success_claim_without_evidence: bool | None = None,
            alarmed_by: str | None = Query(None, pattern=r"^[A-Za-z0-9_.-]{1,80}$"),
            sort: Literal["suspicion", "alarms"] = "suspicion",
            cluster_id: str | None = None,
            limit: int = Query(50, ge=1, le=atlas_view.MAX_PAGE),
            offset: int = Query(0, ge=0),
            split: str = "dev",
        ):
            _require_dev(split)
            filters = atlas_view.AtlasFilters(
                q=q,
                source=source,
                outcome=outcome,
                tests=tests,
                eval_awareness=eval_awareness,
                facet_model=facet_model,
                min_suspicion=min_suspicion,
                grader_or_harness_touched=grader_or_harness_touched,
                answer_material_accessed=answer_material_accessed,
                success_claim_without_evidence=success_claim_without_evidence,
                cluster_id=cluster_id,
                alarmed_by=alarmed_by,
                sort=sort,
            )
            try:
                store, traces = self._atlas_store(), self._atlas_traces()
                shown, _ = atlas_view.visible_facets(store, traces)
                alarms, read, available = self._run_alarms(shown)
                listing = atlas_view.atlas_listing(
                    store,
                    traces,
                    filters,
                    limit=limit,
                    offset=offset,
                    blinding=atlas_coverage.blinding_states(self._atlas_refresh.get(), shown),
                    alarms=alarms,
                    read=read,
                    alarms_available=available,
                )
                page_ids = tuple(row["run_id"] for row in listing["runs"])
                with_transcript = self._derived(
                    "atlas_actor_runs", page_ids, lambda: frozenset(self._actor_runs(page_ids))
                )
                for row in listing["runs"]:
                    row["actor_inputs_available"] = row["run_id"] in with_transcript
                return listing
            except ValueError as error:
                raise HTTPException(400, str(error)) from error

        @self.app.get("/api/cloud-db/atlas/run")
        def atlas_run(run_id: str, split: str = "dev"):
            _require_dev(split)
            return self._atlas_run(run_id)

        @self.app.get("/api/cloud-db/atlas/coverage")
        def atlas_coverage_route(split: str = "dev"):
            _require_dev(split)
            store, traces = self._atlas_store(), self._atlas_traces()
            shown, withheld = atlas_view.visible_facets(store, traces)
            return atlas_coverage.build_coverage(
                store, traces, shown, withheld, self._atlas_refresh.get()
            )

        @self.app.get("/api/cloud-db/atlas/field-reliability")
        def atlas_field_reliability(split: str = "dev"):
            _require_dev(split)
            return self._field_reliability.get()

        @self.app.get("/api/cloud-db/repair-board")
        def repair_board_route(split: str = "dev"):
            _require_dev(split)
            inventory = self._repair_inventory.get()
            task_ids = {row["task_id"] for row in inventory.get("tasks", [])}
            traces = self._atlas_traces(task_ids)
            shown, _ = atlas_view.visible_facets(self._atlas_store(), traces)
            return repair_board.build_board(
                inventory, self._admitted_task_ids() & task_ids, shown, traces
            )

        @self.app.get("/api/cloud-db/cross-test-sets")
        def cross_test_sets_route(split: str = "dev"):
            _require_dev(split)
            return self._cross_test_registry.get()

        @self.app.get("/api/cloud-db/trace-tools")
        def trace_tools(split: str = "dev"):
            _require_dev(split)
            return self._tool_scorecard.get()

        @self.app.get("/api/cloud-db/trace-tools/runs")
        def trace_tool_runs(
            tool: str = Query(min_length=1, max_length=40),
            kind: Literal["hit", "clean", "unread"] = "hit",
            limit: int = Query(50, ge=1, le=tool_runs.MAX_PAGE),
            offset: int = Query(0, ge=0),
            split: str = "dev",
        ):
            _require_dev(split)
            if tool not in tool_runs.RUN_LIST_TOOLS:
                raise HTTPException(404, f"No per-run list is retained for {tool}")
            store, traces = self._atlas_store(), self._atlas_traces()
            shown, _ = atlas_view.visible_facets(store, traces)
            return tool_runs.tool_run_page(
                tool,
                kind,
                traces=traces,
                shown=shown,
                facets_n=len(store.facets),
                findings=self._tool_results,
                limit=limit,
                offset=offset,
            )

        @self.app.get("/api/cloud-db/task-evidence")
        def task_evidence_route(
            task_id: str = Query(min_length=1, max_length=300), split: str = "dev"
        ):
            _require_dev(split)
            return self._task_evidence(task_id)

        @self.app.get("/api/cloud-db/task-evidence/search")
        def task_evidence_search(q: str = Query("", max_length=100), split: str = "dev"):
            _require_dev(split)
            return self._task_search(q)

        @self.app.get("/api/cloud-db/task-evidence/gate-progress")
        def gate_progress_route(split: str = "dev"):
            _require_dev(split)
            return self._gate_progress.get()

        @self.app.get("/api/cloud-db/harness")
        def harness_route(split: str = "dev"):
            _require_dev(split)
            return self._harness_overview()

        @self.app.get("/api/cloud-db/harness/card")
        def harness_card_route(run_id: str = Query(min_length=1, max_length=200), split: str = "dev"):
            _require_dev(split)
            return self._harness_card(run_id)

    def run_supervisor_step(self, approval: str, max_usd: float) -> dict[str, Any]:
        """One explicitly approved cloud step, never a laptop Docker fallback."""
        if not approval.strip():
            raise ValueError("explicit cloud approval is required")
        if isinstance(max_usd, bool) or not math.isfinite(max_usd) or max_usd <= 0:
            raise ValueError("max_usd must be finite and positive")
        with self._step_lock, ExitStack() as cleanup:
            cleanup.callback(self._commit)
            config = ExecutionBackendConfig(
                backend="modal",
                allow_local_docker=False,
                modal_app_name=f"{BILLING_TAG}-workers",
                approval=approval,
                max_usd=max_usd,
                spend_ledger=self.data_root / "cloud-spend.json",
                billing_tag=BILLING_TAG,
            )
            monitor = MisalignmentLiveWorker(emit=self.queue.append_event, registry=self.registry)
            cleanup.callback(monitor.close)
            tq = TaskQualityLiveWorker(
                self.queue, repo_root=self.repo_root, execution_config=config
            )
            seeker = RewardSeekerLiveWorker(
                self.queue,
                repo_root=self.repo_root,
                work_root=self.data_root / "evidence/seeker",
                execution_config=config,
                allow_paid_lanes=False,
            )
            publisher = LocalJetStreamAdapter(self.data_root / "outbox.sqlite")
            cleanup.callback(publisher.close)
            relay = OutboxRelay(self.queue, publisher)
            cleanup.callback(relay.close)
            supervisor = ProductionSupervisor(
                self.queue,
                monitor=monitor,
                task_quality=tq,
                seeker=seeker,
                relay=relay,
                repo_root=self.repo_root,
                supervisor_id="modal-cloud-production",
            )
            cleanup.callback(supervisor.close)
            report = supervisor.run_step()
            return {"backend": "modal", "allow_local_docker": False, **report}

    def close(self) -> None:
        if self._closed:
            return
        with self._step_lock, ExitStack() as cleanup:
            cleanup.callback(self._commit)
            cleanup.callback(self.database.close)
            cleanup.callback(self.queue.close)
            self._closed = True


def runtime_from_environment(
    data_root: Path,
    repo_root: Path,
    environ: Mapping[str, str],
    *,
    commit: Callable[[], None] | None = None,
) -> CloudRuntime:
    """The runtime as the Modal container builds it; the local deploy smoke builds the same one."""
    inventory = environ.get("EVALLAB_REPAIR_INVENTORY")
    registry = environ.get("EVALLAB_CROSS_TEST_REGISTRY")
    scope = environ.get("EVALLAB_DETECTOR_SCOPE")
    progress = environ.get("EVALLAB_GATE_PROGRESS")
    return CloudRuntime(
        data_root,
        repo_root,
        environ["EVALLAB_CLOUD_TOKEN"],
        commit=commit,
        repair_inventory=Path(inventory) if inventory else None,
        cross_test_registry=Path(registry) if registry else None,
        detector_scope=Path(scope) if scope else None,
        gate_progress=Path(progress) if progress else None,
    )


app = modal.App(APP_NAME)
store_volume = modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)
image = (
    modal.Image.debian_slim(python_version="3.12")
    .uv_sync(str(REPO_ROOT), extras=["laminar"], uv_version="0.9.24")
    .env(
        {
            "PYTHONPATH": "/opt/eval-lab/src",
            "EVALLAB_DISABLE_LOCAL_DOCKER": "1",
            "EVALLAB_DERIVED_ROOT": "/data/derived",
            "EVALLAB_SEEKER_LIVE_MODEL": "0",
        }
    )
    .workdir(str(CLOUD_REPO_ROOT))
    .add_local_dir(REPO_ROOT / "src", "/opt/eval-lab/src", ignore=["**/__pycache__", "**/*.pyc"])
    .add_local_dir(
        REPO_ROOT / "library", "/opt/eval-lab/library", ignore=["**/__pycache__", "**/*.pyc"]
    )
    .add_local_dir(
        REPO_ROOT / "research", "/opt/eval-lab/research", ignore=["**/__pycache__", "**/*.pyc"]
    )
    .add_local_dir(REPO_ROOT / "sql", "/opt/eval-lab/sql")
    .add_local_file(REPO_ROOT / "pyproject.toml", "/opt/eval-lab/pyproject.toml")
)


@app.cls(
    image=image,
    volumes={str(DATA_ROOT): store_volume},
    secrets=[modal.Secret.from_name("evallab-cloud-production-auth")],
    cpu=1,
    memory=2048,
    max_containers=1,
    scaledown_window=30,
    timeout=3600,
)
@modal.concurrent(max_inputs=100)
class CloudProduction:
    """One owner for all Volume-backed SQLite state, including worker writes."""

    @modal.enter()
    def start(self):
        # Preserve legacy absolute source references without rewriting immutable
        # queue events. Only explicitly recorded, owned source roots are aliased.
        aliases = DATA_ROOT / "legacy-repo-roots.json"
        if aliases.is_file():
            for value in json.loads(aliases.read_text()):
                path = Path(value)
                if not path.is_absolute() or not (
                    str(path).startswith(
                        "/Users/petermakhnatch/.local/state/evallab-prod-live/releases/"
                    )
                    or str(path) == "/Users/petermakhnatch/Developer/eval-lab"
                ):
                    raise ValueError("unexpected legacy repository root")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(CLOUD_REPO_ROOT, target_is_directory=True)
        # Fresh verification artifacts survive scale-down alongside the queues.
        (DATA_ROOT / "runs").mkdir(exist_ok=True)
        (CLOUD_REPO_ROOT / "runs").symlink_to(DATA_ROOT / "runs", target_is_directory=True)
        self.runtime = runtime_from_environment(
            DATA_ROOT, CLOUD_REPO_ROOT, os.environ, commit=store_volume.commit
        )

    @modal.asgi_app()
    def web(self) -> FastAPI:
        return self.runtime.app

    @modal.method()
    def supervisor_step(self, approval: str, max_usd: float) -> dict[str, Any]:
        return self.runtime.run_supervisor_step(approval, max_usd)

    @modal.exit()
    def stop(self):
        self.runtime.close()


@app.function(image=image, cpu=0.25, memory=256, timeout=3600, max_containers=1)
def run_cloud_supervisor_step(approval: str, max_usd: float) -> dict[str, Any]:
    """Authenticated Modal RPC; all jobs and CPU checks run in the cloud owner."""
    return CloudProduction().supervisor_step.remote(approval, max_usd)
