"""The cumulative view of a live line, and the live checks.

Nothing here is stored. The cumulative view is a batch-shaped scratch folder built on demand
from the committed child batches alone, so the line's own ``build_scoreboard`` produces the
cumulative scoreboard: counts and label joins are summed over runs, and every Wilson interval
is computed on the totals, never averaged across increments.

``line check STATE_DIR`` is read-only for every committed child. It runs the line's own
``check_batch`` on each child and three global checks on top:

* ``conservation``: commit records are 1..N, each one's decisions, child folder and spec hashes
  still match, the increment names its own batch, and no run id appears in two children;
* ``cumulative``: the view builds from the children and its totals are the children's totals;
* ``spend``: the children's total spend is within the cap.
"""

from __future__ import annotations

import json
import tempfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from evallab import rollout_admission as ra
from evallab.line import core
from evallab.line.io import digest, read_json, read_rows
from evallab.line.scoreboard import build_scoreboard
from evallab.line.watch_queue import read_queue_rows
from evallab.line.watch_state import (
    COMMIT_SCHEMA,
    INCREMENT_NAME,
    STATE_SCHEMA,
    State,
    WatchError,
    config_identity,
    frozen_task_status,
    owner_alive,
)

LIVE_CHECK_NAMES = ("children", "conservation", "cumulative", "spend")
MONEY_EPSILON = 1e-9


def _rows_text(rows: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(row, sort_keys=True, allow_nan=False) + "\n" for row in rows)


def _json_text(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"


def child_dir(state: State, record: dict[str, Any]) -> Path:
    return state.root / record["batch"]


def batch_name(config: dict[str, Any], n: int) -> str:
    return f"{config['watch_id']}-{n:06d}"


def child_path(config: dict[str, Any], n: int) -> str:
    return f"increments/{n:06d}/batch/{batch_name(config, n)}"


def union_files(state: State, records: list[dict[str, Any]], dest: Path) -> dict[str, str]:
    """The files of a batch-shaped folder at ``dest`` that are derived from the child batches."""
    config = state.config()
    runs: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    queue: list[dict[str, Any]] = []
    verdicts: list[dict[str, Any]] = []
    tasks: dict[str, dict[str, Any]] = {}
    labels: dict[str, list[dict[str, Any]]] = {}
    stages: dict[str, dict[str, Any]] = {}
    for record in records:
        child = child_dir(state, record)
        runs += read_rows(child / "runs.jsonl")
        decisions += read_rows(child / "decisions.jsonl")
        queue += read_rows(child / "judge-queue.jsonl")
        verdicts += read_rows(child / "verdicts.jsonl")
        for row in read_rows(child / "tasks.jsonl"):
            if tasks.setdefault(row["task_id"], row) != row:
                raise ValueError(f"increments disagree about task {row['task_id']!r}")
        for source in read_json(child / "spec.json")["sources"]:
            if source.get("labels"):
                labels.setdefault(source["corpus"], []).extend(read_rows(Path(source["labels"])))
        for path in sorted((child / "receipts").glob("*.json")):
            receipt = read_json(path)
            stage = stages.setdefault(
                receipt["stage"],
                {
                    "schema": "evallab.line_receipt/v1",
                    "batch_id": config["watch_id"],
                    "stage": receipt["stage"],
                    "counts": {"rows_in": 0, "rows_out": 0},
                    "seconds": 0.0,
                    "spend_usd": 0.0,
                    "models": [],
                    "errors": [],
                    "increments": 0,
                },
            )
            stage["counts"]["rows_in"] += receipt["counts"]["rows_in"]
            stage["counts"]["rows_out"] += receipt["counts"]["rows_out"]
            stage["seconds"] += receipt["seconds"]
            stage["spend_usd"] += receipt.get("spend_usd", 0)
            stage["errors"] += receipt.get("errors", [])
            for model in receipt.get("models", []):
                if model not in stage["models"]:
                    stage["models"].append(model)
            stage["increments"] += 1

    line = config["line"]
    files = {
        "spec.json": _json_text(
            {
                "schema": "evallab.line_spec/v1",
                "batch_id": config["watch_id"],
                "split": "dev",
                "sources": [
                    {
                        "corpus": source["corpus"],
                        "rollouts": source["path"],
                        "labels": str(dest / "labels" / f"{source['corpus']}.jsonl")
                        if source["labels"]
                        else None,
                    }
                    for source in config["sources"]
                ],
                "labels_policy": line["labels_policy"],
                "autoreview": {"max_usd": line["autoreview"]["max_usd"]},
                "increments": [r["n"] for r in records],
            }
        ),
        "runs.jsonl": _rows_text(runs),
        "decisions.jsonl": _rows_text(decisions),
        "judge-queue.jsonl": _rows_text(queue),
        "verdicts.jsonl": _rows_text(verdicts),
        "tasks.jsonl": _rows_text([tasks[key] for key in sorted(tasks)]),
    }
    for source in config["sources"]:
        if source["labels"]:
            ordered = sorted(labels.get(source["corpus"], []), key=lambda row: str(row["id"]))
            files[f"labels/{source['corpus']}.jsonl"] = _rows_text(ordered)
    for stage_name, receipt in sorted(stages.items()):
        files[f"receipts/{stage_name}.json"] = _json_text(receipt)
    return files


def cumulative_scoreboard(
    state: State, records: list[dict[str, Any]], repo_root: Path
) -> dict[str, Any]:
    """The cumulative scoreboard, built on demand from the committed children in a scratch folder.

    Nothing derived is kept on disk. Counts and label joins are summed over runs and every
    Wilson interval is computed on the totals by the line's own ``build_scoreboard``.
    """
    with tempfile.TemporaryDirectory(prefix="line-cumulative-") as scratch:
        dest = Path(scratch)
        for name, text in union_files(state, records, dest).items():
            path = dest / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return build_scoreboard(dest, repo_root)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _gate(checks: list[dict[str, str]], name: str, function: Callable[[], None]) -> None:
    try:
        function()
        checks.append({"name": name, "status": "PASS", "details": ""})
    except (
        ValueError,
        OSError,
        KeyError,
        TypeError,
        AssertionError,
        AttributeError,
        IndexError,
        WatchError,
    ) as error:
        checks.append(
            {"name": name, "status": "FAIL", "details": str(error) or type(error).__name__}
        )


def _check_children(
    state: State, repo_root: Path, records: list[dict[str, Any]], *, deep: bool
) -> None:
    """The line's own check on each child (the newest only unless ``deep``). Reads, never writes."""
    for record in records if deep else records[-1:]:
        result = core.check_batch(child_dir(state, record), repo_root)
        failed = [c["name"] for c in result["checks"] if c["status"] != "PASS"]
        _require(not failed, f"child {record['batch_id']} fails {failed}")


def _check_conservation(
    state: State, config: dict[str, Any], records: list[dict[str, Any]]
) -> None:
    names = [p.name for p in state.folders()]
    wanted = [f"{n:06d}" for n in range(1, len(records) + 1)]
    running = [f"{len(records) + 1:06d}"] if owner_alive(state) else []  # one open, while it runs
    _require(
        names[: len(wanted)] == wanted and names[len(wanted) :] in ([], running),
        f"increments/ is not exactly the commits 1..N: {names[:5]}",
    )
    stored = _check_watch_json(state)
    seen: Counter[str] = Counter()
    for number, record in enumerate(records, 1):
        _require(
            record.get("schema") == COMMIT_SCHEMA, f"increment {number}: unknown commit schema"
        )
        _require(
            record.get("n") == number, f"increment {number}: commit record names {record.get('n')}"
        )
        _require(
            record.get("batch") == child_path(config, number)
            and record.get("batch_id") == batch_name(config, number),
            f"increment {number}: commit record names another batch",
        )
        child = child_dir(state, record)
        _require(
            child.is_dir() and not child.is_symlink(), f"increment {number}: child batch missing"
        )
        _require(
            digest(child / "decisions.jsonl") == record.get("decisions_sha256"),
            f"increment {number}: decisions changed after commit",
        )
        _require(
            digest(child) == record.get("batch_sha256"),
            f"increment {number}: a file of the child batch changed after commit",
        )
        _require(
            digest(state.inc(number) / "spec.json") == record.get("spec_sha256"),
            f"increment {number}: its spec.json changed after commit",
        )
        child_ids = [row["run_id"] for row in read_rows(child / "runs.jsonl")]
        seen.update(child_ids)
        _require(
            isinstance(record.get("runs"), dict) and sorted(record["runs"]) == sorted(child_ids),
            f"increment {number}: the commit record's runs are not the child's runs",
        )
        _require(
            isinstance(record.get("times"), dict)
            and all(isinstance(run, dict) for run in record["runs"].values()),
            f"increment {number}: the commit record's times or runs are malformed",
        )
        _require(
            not ra.declares_non_dev(record), f"increment {number}: the commit record names a split"
        )
        core.reject_label_fields(record, f"commit record {number}")
        _require(
            record.get("config_sha256") == stored["config_sha256"]
            and record.get("task_status_sha256") == stored["frozen_task_status_sha256"],
            f"increment {number}: committed under another configuration or task status",
        )
    repeated = sorted(run_id for run_id, count in seen.items() if count > 1)
    _require(not repeated, f"run in two increments: {repeated[:3]}")
    _check_queue(state, config, records)


def _check_watch_json(state: State) -> dict[str, Any]:
    """``watch.json`` is what the state was created with and still names the frozen task status."""
    stored = read_json(state.watch_json)
    _require(stored.get("schema") == STATE_SCHEMA, "watch.json has an unknown schema")
    _require(
        stored.get("config_sha256") == config_identity(stored["config"]),
        "watch.json's configuration no longer matches its recorded hash",
    )
    frozen = frozen_task_status(state)
    _require(
        (digest(frozen) if frozen else None) == stored.get("frozen_task_status_sha256"),
        "the frozen task status no longer matches the hash recorded in watch.json",
    )
    return stored


def _check_queue(state: State, config: dict[str, Any], records: list[dict[str, Any]]) -> None:
    """The Judge queue this watcher owns holds every committed child's queue rows once, no others.

    The watcher queues an increment's rows just before it commits, so a crash (or a running
    watcher) can leave rows of an increment that is open or was moved to ``abandoned/``; exactly
    those are tolerated, and they are queued once whichever increment finally commits them.
    """
    if not config["judge"]["write"]:
        return
    queued = read_queue_rows(Path(config["judge"]["queue"]))
    by_id: dict[str, dict[str, Any]] = {}
    for row in queued:
        core.reject_label_fields(row, "judge queue row")
        _require(not ra.declares_non_dev(row), f"queue row {row['run_id']!r} names a split")
        _require(row["run_id"] not in by_id, f"queue holds {row['run_id']!r} twice")
        by_id[row["run_id"]] = row
    expected: dict[str, dict[str, Any]] = {}
    for record in records:
        for row in read_rows(child_dir(state, record) / "judge-queue.jsonl"):
            expected[row["run_id"]] = row
    for run_id, row in sorted(expected.items()):
        _require(run_id in by_id, f"queue is missing {run_id!r}, routed by a committed increment")
        _require(
            by_id[run_id] == row,
            f"queue row for {run_id!r} differs from the row its increment produced",
        )
    tolerated: set[str] = set()
    leftovers = list(state.abandoned.glob("*/batch/*/judge-queue.jsonl"))
    if owner_alive(state):
        for folder in state.open_folders():
            leftovers += folder.glob("batch/*/judge-queue.jsonl")
    for path in leftovers:
        tolerated.update(row["run_id"] for row in read_rows(path))
    extra = sorted(set(by_id) - set(expected) - tolerated)
    _require(not extra, f"queue holds runs no committed increment routed: {extra[:3]}")


def _check_cumulative(state: State, records: list[dict[str, Any]], repo_root: Path) -> None:
    """The on-demand cumulative view builds, and its totals are the children's totals."""
    overall = cumulative_scoreboard(state, records, repo_root)["overall"]
    children = [read_json(child_dir(state, r) / "scoreboard.json")["overall"] for r in records]
    counts: Counter[str] = Counter()
    for child in children:
        counts.update(child["counts"])
    expected = {
        "runs": sum(child["runs"] for child in children),
        "labelled_runs": sum(child["labelled_runs"] for child in children),
        "cheats": sum(child["cheats"] for child in children),
        "clean": sum(child["clean"] for child in children),
        "counts": {key: counts.get(key, 0) for key in overall["counts"]},
    }
    _require(
        {key: overall[key] for key in expected} == expected,
        "the cumulative scoreboard's totals differ from the sums of the child scoreboards",
    )
    _require(
        overall["runs"] == sum(len(r["runs"]) for r in records),
        "the cumulative scoreboard does not cover exactly the committed runs",
    )


def _check_spend(state: State, config: dict[str, Any], records: list[dict[str, Any]]) -> None:
    spent = sum(
        float(read_json(child_dir(state, r) / "scoreboard.json")["spend"]["usd"]) for r in records
    )
    cap = config["line"]["autoreview"]["max_usd"]
    _require(spent <= cap + MONEY_EPSILON, f"total spend {spent} exceeds the cap {cap}")


def verify(state: State, repo_root: Path, *, deep: bool) -> dict[str, Any]:
    """The live checks, reading every declared file the way the line reads it."""
    with core.declared_scope(repo_root):
        return _verify(state, repo_root, deep=deep)


def _verify(state: State, repo_root: Path, *, deep: bool) -> dict[str, Any]:
    """Run the live checks. Anything unreadable fails its check with the reason; nothing raises.

    Committed children and the state are only read. ``deep`` runs ``check_batch`` on every
    child, otherwise only on the newest one; the other checks always cover every child.
    """
    checks: list[dict[str, str]] = []
    records: list[dict[str, Any]] = []
    config: dict[str, Any] = {}
    unreadable = ""
    try:
        records, config = state.committed(), state.config()
    except (ValueError, OSError, KeyError, TypeError) as error:
        unreadable = f"cannot read the commit records or watch.json: {error}"

    def run(function: Callable[[], None]) -> Callable[[], None]:
        def guarded() -> None:
            if unreadable:
                raise ValueError(unreadable)
            function()

        return guarded

    _gate(checks, "children", run(lambda: _check_children(state, repo_root, records, deep=deep)))
    _gate(checks, "conservation", run(lambda: _check_conservation(state, config, records)))
    _gate(checks, "cumulative", run(lambda: _check_cumulative(state, records, repo_root)))
    _gate(checks, "spend", run(lambda: _check_spend(state, config, records)))
    return {
        "checks": checks,
        "passed": all(c["status"] == "PASS" for c in checks),
        "deep": deep,
        "increments": len(records),
        "runs": sum(len(r["runs"]) for r in records if isinstance(r.get("runs"), dict)),
    }


__all__ = [
    "INCREMENT_NAME",
    "LIVE_CHECK_NAMES",
    "batch_name",
    "child_dir",
    "child_path",
    "cumulative_scoreboard",
    "verify",
]
