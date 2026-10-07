"""Transient DuckDB trial census (HAR-178).

One in-memory ``trials`` view over local Harbor runs: every trial found
under the main/worktree runs and jobs roots -- finished, infra, and
unfinished -- left-joined to its existing per-native-pair projections.
No new persistent store, no network, no PostgreSQL.

The view DDL lives once in ``sql/trials.sql`` (loaded here as the actual
statement, following the ``database.py`` SQL-asset convention). Pipeline
order matters at census scale: cheap discovery first, dedupe by native
pair second, full posture/projection reads only for survivors.

* Identity is the native ``(job_id, trial_id)`` pair. The per-trial
  ``result["config"]["job_id"]`` (correct even under aggregate hardlink
  viewers) precedes the parent job directory id; the parent id is the
  legacy fallback. Copies sharing a pair dedupe to one row, preferring
  the recorded ``trial_uri`` original, then the canonical parent match
  outside viewer overlays, then scan-root precedence. Same names with
  distinct ids stay distinct rows. Incomplete trials keep a row with
  stable path identity and nullable ids; unknown or unsafe ids never
  reach projection paths or the filesystem.
* Discovery reads only small metadata. Traversal prunes under recognized
  trials; dot bookkeeping and symlinked directories are never descended.
* Facts come from existing parquet via
  ``storage.paths.discover_parquet_partitions``: hot files addressed per
  pair, other supported layouts (job, revision, cold-*) joined natively
  on ``(job_id, trial_id)`` in one query per table. Per-pair rows are
  validated (trial_facts exactly one, reward names unique, features one
  row; a present-but-empty reward file is a valid unscored trial, while
  a file with only foreign rows is not a present projection).
* Gaps trigger one cached ``process_job`` backfill per job with reports
  isolated in a ``TemporaryDirectory``; only parquet projections persist.
  After success the shared target reads first; after failure only
  freshness-proven values survive, everything else is an explicit error.
* Posture and cut-short come straight from
  ``evallab.interpretation.trial_posture`` (pinned dependency, direct
  calls); ``reward`` is the exact ``reward`` dimension with a
  ``primary_reward`` fallback, integrity/gated strictly theirs, and
  stop/copy from the stored HAR-159 features.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from evallab.harbor_watch_hooks import read_hook_journal
from evallab.interpretation.trial_posture import cut_short_by_our_limits, trial_posture
from evallab.process_job import process_job
from evallab.storage.paths import (
    ParquetPartition,
    derived_root_from_environment,
    discover_parquet_partitions,
    trial_parquet_path,
    trials_derived_roots,
    trials_roots,
)

_TRIALS_SQL = (
    Path(__file__).resolve().parents[3] / "sql" / "trials.sql"
).read_text(encoding="utf-8")

#: Non-hot layouts joined natively for the fact tables (features are hot-only:
#: their rows carry names, not native ids, so only the per-pair path binds them).
_JOIN_LAYOUTS = ("job", "revision", "cold-table", "cold-day", "directory", "root")

_FACT_TABLES = ("trial_facts", "reward_facts")
type HotPartitions = dict[tuple[str, str, str], list[ParquetPartition]]


def _resolve(root: Path, path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _is_trial_result(payload: dict[str, Any]) -> bool:
    """A real trial result, regardless of job completion or ATIF presence."""
    return "task_name" in payload and "trial_name" in payload


def _is_job_shape(payload: dict[str, Any]) -> bool:
    """A job directory, finished or still running.

    Unfinished native jobs (timestamp names carrying ``__``, with
    ``config.json``) must descend to their trials, never be mistaken for
    a trial themselves: ``finished_at`` is completion evidence, not shape.
    """
    return "n_total_trials" in payload and "stats" in payload


def _trajectory_present(trial_dir: Path) -> bool:
    return (trial_dir / "agent" / "trajectory.json").is_file() or (
        trial_dir / "trajectory.json"
    ).is_file()


def _hook_trials(job_dir: Path, cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
    key = str(job_dir)
    if key not in cache:
        cache[key] = read_hook_journal(job_dir)
    return cache[key]


def _discover_inventory(
    roots: Sequence[Path],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Cheap trial inventory; returns ``(rows, unreadable_result_json_paths)``.

    Prunes under recognized trials, never descends dot bookkeeping or
    symlinked directories, and never aborts on malformed evidence.
    """
    inventory: list[dict[str, Any]] = []
    seen: set[str] = set()
    counted_bad: set[str] = set()
    unreadable: list[str] = []
    hook_cache: dict[str, dict[str, Any]] = {}
    for root_index, raw_root in enumerate(roots):
        try:
            resolved_root = Path(raw_root).expanduser().resolve()
        except OSError:
            continue
        if not resolved_root.is_dir():
            continue
        stack: list[Path] = [resolved_root]
        while stack:
            current = stack.pop()
            try:
                resolved = current.resolve()
            except OSError:
                continue
            result_path = resolved / "result.json"
            payload: dict[str, Any] | None = None
            if result_path.is_file():
                payload = _read_json_object(result_path)
                if payload is None and str(result_path) not in counted_bad:
                    counted_bad.add(str(result_path))
                    unreadable.append(str(result_path))
            if isinstance(payload, dict) and _is_trial_result(payload):
                key = str(resolved)
                if key not in seen:
                    seen.add(key)
                    inventory.append(
                        {
                            "trial_dir": resolved,
                            "job_dir": resolved.parent,
                            "result": payload,
                            "root_index": root_index,
                        }
                    )
                continue  # prune: trial contents are never trials
            if resolved != resolved_root and not (
                isinstance(payload, dict) and _is_job_shape(payload)
            ):
                hooked = _hook_trials(resolved.parent, hook_cache)
                config_present = (resolved / "config.json").is_file()
                hook_proves = resolved.name in hooked
                if (config_present or hook_proves or _trajectory_present(resolved)) and (
                    "__" in resolved.name or hook_proves
                ):
                    key = str(resolved)
                    if key not in seen:
                        seen.add(key)
                        inventory.append(
                            {
                                "trial_dir": resolved,
                                "job_dir": resolved.parent,
                                "result": None,
                                "root_index": root_index,
                            }
                        )
                    continue  # prune under a recognized (incomplete) trial
            try:
                children = sorted(resolved.iterdir())
            except OSError:
                continue
            for child in children:
                try:
                    if child.is_symlink() or not child.is_dir():
                        continue
                except OSError:
                    continue
                if child.name.startswith("."):
                    continue
                try:
                    relative = child.relative_to(resolved_root)
                except ValueError:
                    continue
                if any(part.startswith(".") for part in relative.parts):
                    continue
                stack.append(child)
    inventory.sort(key=lambda row: str(row["trial_dir"]))
    return inventory, sorted(unreadable)


def _safe_identity(value: Any) -> str | None:
    """A native id safe to interpolate into a ``job_id=``/``trial_id=`` path."""
    if not isinstance(value, str) or not value:
        return None
    if value in (".", ".."):
        return None
    if "/" in value or "\\" in value or "\x00" in value:
        return None
    if Path(value).name != value:
        return None
    return value


def _native_ids(
    row: dict[str, Any], parent_cache: dict[str, dict[str, Any]]
) -> tuple[str | None, str | None, str | None, bool]:
    """``(job_id, trial_id, parent_job_id, unsafe)``; config precedes parent id."""
    result = row.get("result") or {}
    trial_id = _safe_identity(result.get("id"))
    unsafe = "id" in result and trial_id is None and result.get("id") is not None
    config = result.get("config")
    config_job = _safe_identity(config.get("job_id")) if isinstance(config, dict) else None
    if isinstance(config, dict) and config.get("job_id") is not None and config_job is None:
        unsafe = True
    job_dir = row["job_dir"]
    parent_key = str(job_dir)
    if parent_key not in parent_cache:
        parent_cache[parent_key] = _read_json_object(job_dir / "result.json") or {}
    parent_job_id = _safe_identity(parent_cache[parent_key].get("id"))
    return config_job or parent_job_id, trial_id, parent_job_id, unsafe


def _uri_path(result: dict[str, Any] | None) -> Path | None:
    """The recorded original trial location, when it parses as a file URI."""
    uri = (result or {}).get("trial_uri")
    if not isinstance(uri, str):
        return None
    try:
        parsed = urlparse(uri)
    except ValueError:
        return None
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
        return None
    try:
        return Path(unquote(parsed.path)).resolve()
    except (OSError, RuntimeError):
        return None


def _viewer_marked(job_dir: Path, trial_dir: Path) -> bool:
    try:
        return (job_dir / ".evallab-source.json").is_file() or (
            trial_dir / ".evallab-source.json"
        ).is_file()
    except OSError:
        return False


def _dedupe_inventory(inventory: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedupe physical copies by native pair, preferring the recorded original.

    Unknown or unsafe ids keep stable path identity and are never merged.
    Survivors carry ``job_id``, ``trial_id``, ``parent_job_id``, and
    ``unsafe_identity`` keys.
    """
    parent_cache: dict[str, dict[str, Any]] = {}
    # Published copies repeat the same URI; resolve it once per lookup, not copy.
    uri_cache: dict[str, Path | None] = {}
    for row in inventory:
        job_id, trial_id, parent_job_id, unsafe = _native_ids(row, parent_cache)
        row["job_id"], row["trial_id"] = job_id, trial_id
        row["parent_job_id"], row["unsafe_identity"] = parent_job_id, unsafe
        uri = (row.get("result") or {}).get("trial_uri")
        if isinstance(uri, str):
            if uri not in uri_cache:
                uri_cache[uri] = _uri_path(row.get("result"))
            row["uri_path"] = uri_cache[uri]
        else:
            row["uri_path"] = None
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    survivors: list[dict[str, Any]] = []
    for row in inventory:
        if row["job_id"] is not None and row["trial_id"] is not None and not row["unsafe_identity"]:
            grouped.setdefault((row["job_id"], row["trial_id"]), []).append(row)
        else:
            survivors.append(row)

    def _rank(row: dict[str, Any]) -> tuple[int, int, int, int, str]:
        uri_match = row["uri_path"] is not None and row["uri_path"] == row["trial_dir"]
        parent_match = (
            row["parent_job_id"] is not None and row["parent_job_id"] == row["job_id"]
        )
        return (
            0 if uri_match else 1,
            0 if parent_match else 1,
            0 if not _viewer_marked(row["job_dir"], row["trial_dir"]) else 1,
            row["root_index"],
            str(row["trial_dir"]),
        )

    for pair in sorted(grouped):
        survivors.append(sorted(grouped[pair], key=_rank)[0])
    survivors.sort(key=lambda row: str(row["trial_dir"]))
    return survivors


def _read_hot_file(path: Path) -> tuple[str, list[dict[str, Any]], str | None]:
    """Read one hot parquet file: ``(status, rows, error)``."""
    try:
        rows = pq.ParquetFile(path).read().to_pylist()
    except Exception as exc:  # noqa: BLE001 -- malformed projections are row errors
        return "unreadable", [], f"unreadable {path.name}: {type(exc).__name__}: {exc}"
    return "read", rows, None


def _validate_table_rows(
    table: str, all_rows: list[dict[str, Any]], pair_rows: list[dict[str, Any]]
) -> tuple[str, Any, str | None]:
    """Validate native-bound rows: ``(status, payload, error)``.

    A present-but-empty reward file is a valid unscored trial; a file with
    only foreign rows is not a present projection; conflicting pair rows
    are explicit errors, never silent first-picks.
    """
    if table == "trial_facts":
        if not all_rows or not pair_rows:
            return "absent", None, None
        if len(pair_rows) > 1:
            return "conflict", None, "conflicting trial_facts rows for one native pair"
        return "ok", pair_rows[0], None
    if table == "reward_facts":
        if not all_rows:
            return "ok", [], None
        if not pair_rows:
            return "absent", None, None
        names = [r.get("reward_name") for r in pair_rows]
        if len(set(names)) != len(names):
            return "conflict", None, "conflicting reward_facts reward names for one pair"
        return "ok", pair_rows, None
    if not all_rows:
        return "absent", None, None
    if len(all_rows) > 1:
        return "conflict", None, "conflicting features rows for one native pair"
    return "ok", all_rows[0], None


def _hot_partitions(
    hot: HotPartitions, target_root: Path, table: str, job_id: str, trial_id: str
) -> list[ParquetPartition]:
    """Include new backfill files without rescanning the entire Parquet lake."""
    path = trial_parquet_path(target_root, job_id, trial_id, table)
    candidates = hot.get((table, job_id, trial_id), [])
    target = (
        [ParquetPartition(path, table, "hot", job_id=job_id, trial_id=trial_id)]
        if path.is_file() else []
    )
    return target + [partition for partition in candidates if partition.path != path]


def _sql_literal_list(paths: Sequence[Path]) -> str:
    return "[" + ", ".join("'" + str(p).replace("'", "''") + "'" for p in paths) + "]"


def _join_fact_rows(
    con: duckdb.DuckDBPyConnection,
    discos: Sequence[Any],
    table: str,
    pairs: Sequence[tuple[str, str]],
) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], str | None]:
    """One native join per fact table over the non-hot supported layouts.

    Returns ``(rows_by_pair, error)``; any failure yields ``({}, error)``
    for info-level reporting, never per-row contamination.
    """
    found: dict[tuple[str, str], list[dict[str, Any]]] = {}
    if not pairs:
        return found, None
    files = sorted(
        {
            partition.path
            for disco in discos
            for partition in disco.partitions
            if partition.table == table and partition.layout in _JOIN_LAYOUTS
        },
        key=str,
    )
    if not files:
        return found, None
    needed = pa.table(
        {
            "job_id": [job_id for job_id, _ in pairs],
            "trial_id": [trial_id for _, trial_id in pairs],
        }
    )
    con.register("needed_pairs", needed)
    try:
        cursor = con.execute(
            "SELECT f.* FROM read_parquet("
            + _sql_literal_list(files)
            + ', union_by_name=true) f JOIN "needed_pairs" n'
            ' ON f."job_id" = n."job_id" AND f."trial_id" = n."trial_id"'
        )
        columns = [d[0] for d in cursor.description]
        for values in cursor.fetchall():
            record = dict(zip(columns, values, strict=False))
            if record.get("job_id") is None or record.get("trial_id") is None:
                continue
            found.setdefault(
                (str(record["job_id"]), str(record["trial_id"])), []
            ).append(record)
    except Exception as exc:  # noqa: BLE001 -- lake failures backfill, never omit
        return {}, f"{table} lake join failed: {type(exc).__name__}: {exc}"
    finally:
        with contextlib.suppress(Exception):
            con.unregister("needed_pairs")
    return found, None


def _source_mtime(row: dict[str, Any]) -> float | None:
    mtimes: list[float] = []
    for path in (
        row["trial_dir"] / "result.json",
        row["trial_dir"] / "config.json",
        row["job_dir"] / "result.json",
    ):
        try:
            if path.is_file():
                mtimes.append(path.stat().st_mtime)
        except OSError:
            continue
    return max(mtimes) if mtimes else None


def _pick_table(
    hot: HotPartitions,
    target_root: Path,
    cold: dict[str, dict[tuple[str, str], list[dict[str, Any]]]],
    table: str,
    job_id: str,
    trial_id: str,
    source_mtime: float | None,
) -> tuple[Any, bool, list[str], list[str]]:
    """Validated per-pair payload: ``(payload|None, fresh, problems, notes)``.

    Hot copies are tried in disco order (shared target first); the first
    valid copy wins and rot in losing copies stays store-level notes. Only
    an unresolved table yields per-row problems.
    """
    notes: list[str] = []
    for partition in _hot_partitions(hot, target_root, table, job_id, trial_id):
        status, all_rows, error = _read_hot_file(partition.path)
        if status == "unreadable":
            notes.append(f"{table} {error}")
            continue
        if table == "features":
            status, payload, error = _validate_table_rows(table, all_rows, [])
        else:
            matched = [
                record
                for record in all_rows
                if str(record.get("job_id")) == job_id
                and str(record.get("trial_id")) == trial_id
            ]
            status, payload, error = _validate_table_rows(table, all_rows, matched)
        if error is not None:
            notes.append(f"{table} {error}")
        if status != "ok":
            continue
        fresh = True
        if source_mtime is not None:
            try:
                fresh = partition.path.stat().st_mtime >= source_mtime
            except OSError:
                fresh = False
        if not fresh:
            notes.append(f"stale projections: {table}")
            continue
        return payload, True, [], notes
    if table in _FACT_TABLES:
        cold_rows = cold.get(table, {}).get((job_id, trial_id))
        if cold_rows is not None:
            matched = [
                record
                for record in cold_rows
                if str(record.get("job_id")) == job_id
                and str(record.get("trial_id")) == trial_id
            ]
            status, payload, error = _validate_table_rows(table, cold_rows, matched)
            if error is not None:
                return None, False, [f"{table} {error}"], notes
            if status == "ok":
                return payload, True, [], notes
    fatal = [
        note for note in notes
        if "conflict" in note or "unreadable" in note or "stale projections:" in note
    ]
    return None, False, fatal or [f"missing projections: {table}"], notes


def _resolve_projections(
    *,
    row: dict[str, Any],
    job_id: str,
    trial_id: str,
    hot: HotPartitions,
    target_root: Path,
    cold: dict[str, dict[tuple[str, str], list[dict[str, Any]]]],
) -> tuple[dict[str, Any], set[str], list[str], list[str]]:
    """Validated fact/feature values with trust and problem sets.

    Returns ``(values, trusted_tables, problems, notes)``. ``trusted``
    holds tables with freshness-proven data (fresh hot files or
    lake-joined rows); only those survive a failed backfill as valid.
    """
    values: dict[str, Any] = {
        "reward": None,
        "integrity": None,
        "reward_gated": None,
        "stop_reason": None,
        "copy_verdict": None,
        "primary_reward": None,
    }
    trusted: set[str] = set()
    problems: list[str] = []
    notes: list[str] = []
    source_mtime = _source_mtime(row)
    picked: dict[str, Any] = {}
    for table in ("trial_facts", "reward_facts", "features"):
        payload, fresh, table_problems, table_notes = _pick_table(
            hot, target_root, cold, table, job_id, trial_id, source_mtime
        )
        problems.extend(table_problems)
        notes.extend(table_notes)
        if payload is None:
            continue
        picked[table] = payload
        if fresh:
            trusted.add(table)
    trial_row = picked.get("trial_facts")
    if isinstance(trial_row, dict):
        primary = trial_row.get("primary_reward")
        if isinstance(primary, (int, float)) and not isinstance(primary, bool):
            values["primary_reward"] = float(primary)
    reward_rows = picked.get("reward_facts")
    if isinstance(reward_rows, list):
        dims: dict[str, float] = {}
        for record in reward_rows:
            name, value = record.get("reward_name"), record.get("reward_value")
            if (
                isinstance(name, str)
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and name not in dims
            ):
                dims[name] = float(value)
        values["reward"] = dims.get("reward")
        values["integrity"] = dims.get("integrity")
        values["reward_gated"] = dims.get("reward_gated")
    feature_row = picked.get("features")
    if isinstance(feature_row, dict):
        stop = feature_row.get("stop_reason")
        values["stop_reason"] = str(stop) if stop is not None else None
        verdict = feature_row.get("copy_verdict")
        values["copy_verdict"] = str(verdict) if verdict is not None else None
    if values["reward"] is None:
        values["reward"] = values["primary_reward"]
    return values, trusted, problems, notes


def _null_untrusted(values: dict[str, Any], trusted: set[str]) -> None:
    """Drop values without freshness proof after a failed backfill in place."""
    if "trial_facts" not in trusted:
        values["primary_reward"] = None
    if "reward_facts" not in trusted:
        values["reward"] = values["integrity"] = values["reward_gated"] = None
    if values["reward"] is None and "trial_facts" in trusted:
        values["reward"] = values["primary_reward"]
    if "features" not in trusted:
        values["stop_reason"] = values["copy_verdict"] = None


def _backfill_job(
    *,
    repo_root: Path,
    job_dir: Path,
    target_root: Path,
    cache: dict[str, str | None],
) -> str | None:
    """One cached backfill attempt per job; returns an error string or None."""
    key = str(job_dir)
    if key not in cache:
        target_root.resolve().mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix="evallab-trials-backfill-") as tmp:
                process_job(
                    str(job_dir),
                    root=str(repo_root),
                    output_dir=tmp,
                    ingest=False,
                    publish=False,
                    parquet_root=str(target_root.resolve()),
                )
        except Exception as exc:  # noqa: BLE001 -- backfill gaps are row errors
            cache[key] = f"backfill failed: {type(exc).__name__}: {exc}"
        else:
            cache[key] = None
    return cache[key]


def _read_only_projection(
    row: dict[str, Any], values: dict[str, Any], trusted: set[str]
) -> str | None:
    """Fill cache gaps with the same deterministic producers, in memory only."""
    from evallab.interpretation.features import trial_features
    from evallab.results import load_trial

    _null_untrusted(values, trusted)
    try:
        if not {"trial_facts", "reward_facts"} <= trusted:
            trial = load_trial(row["trial_dir"])
            if "trial_facts" not in trusted:
                values["primary_reward"] = trial.primary_reward
            if "reward_facts" not in trusted:
                values["reward"] = trial.primary_reward
                values["integrity"] = trial.rewards.get("integrity")
                values["reward_gated"] = trial.rewards.get("reward_gated")
            elif values["reward"] is None:
                values["reward"] = values["primary_reward"]
        if "features" not in trusted:
            features = trial_features(row["job_dir"], row["trial_dir"])
            values["stop_reason"] = features["stop_reason"]
            values["copy_verdict"] = features["copy_verdict"]
    except Exception as exc:  # noqa: BLE001 -- preserve the trial and its available evidence
        return f"read-only projection failed: {type(exc).__name__}: {exc}"
    return None


def connect_trials(
    *,
    repo_root: Path,
    roots: Sequence[Path] | None = None,
    derived_root: Path | None = None,
    task_names: Sequence[str] | None = None,
    read_only: bool = False,
) -> tuple[duckdb.DuckDBPyConnection, dict[str, Any]]:
    """Build the transient ``trials`` view; the caller closes the connection.

    ``roots=None`` scans the main/worktree roots; an explicit ``roots``
    (even empty) is the whole selection. ``derived_root`` overrides the
    projection roots and becomes the backfill target, otherwise reads fan
    out over the existing derived roots while backfills write to the
    shared selected store. Keep ``info`` alive while the connection is
    open (it holds the registered Arrow table).

    ``task_names`` restricts the native task names before projection work.
    ``read_only`` forbids backfill writes: cache gaps are projected transiently
    from the same raw evidence producers, with unavailable evidence left null.
    """
    repo = Path(repo_root).resolve()
    scan_roots = [_resolve(repo, Path(p)) for p in roots] if roots is not None else list(trials_roots(repo))
    target_root = derived_root_from_environment(repo, explicit=derived_root)
    read_roots = [target_root] if derived_root is not None else list(trials_derived_roots(repo))
    ordered_roots = [target_root] + [
        root for root in read_roots if Path(root).resolve() != target_root.resolve()
    ]
    con = duckdb.connect(
        ":memory:",
        config={"autoinstall_known_extensions": False, "autoload_known_extensions": False},
    )
    try:
        inventory, unreadable = _discover_inventory(scan_roots)
        rows = _dedupe_inventory(inventory)
        if task_names is not None:
            selected = set(task_names)
            rows = [
                row for row in rows
                if isinstance((row.get("result") or {}).get("task_name"), str)
                and row["result"]["task_name"] in selected
            ]
        selected_jobs = sorted({
            row["job_id"] for row in rows
            if row["job_id"] is not None and not row["unsafe_identity"]
        })
        discos = [
            discover_parquet_partitions(
                Path(root),
                tables=(*_FACT_TABLES, "features"),
                job_ids=selected_jobs,
            )
            for root in ordered_roots
        ] if rows else []
        hot: HotPartitions = {}
        for disco in discos:
            for partition in disco.partitions:
                if partition.layout == "hot" and partition.job_id and partition.trial_id:
                    hot.setdefault(
                        (partition.table, partition.job_id, partition.trial_id), []
                    ).append(partition)
        valid_pairs = sorted(
            {
                (row["job_id"], row["trial_id"])
                for row in rows
                if row["job_id"] is not None and row["trial_id"] is not None
            }
        )
        cold: dict[str, dict[tuple[str, str], list[dict[str, Any]]]] = {}
        lake_errors: list[str] = []
        for table in _FACT_TABLES:
            table_rows, table_error = _join_fact_rows(con, discos, table, valid_pairs)
            cold[table] = table_rows
            if table_error is not None:
                lake_errors.append(table_error)
        backfill_cache: dict[str, str | None] = {}
        records: list[dict[str, Any]] = []
        backfilled: list[str] = []
        in_memory: list[str] = []
        projection_errors: dict[str, str] = {}
        source_notes: set[str] = set()
        for row in rows:
            trial_dir, job_dir = row["trial_dir"], row["job_dir"]
            posture = trial_posture(
                repo_root=repo,
                job_dir=job_dir,
                trial_dir=trial_dir,
                result=row.get("result") or {},
            )
            job_id, trial_id = row["job_id"], row["trial_id"]
            if job_id is None or trial_id is None or row["unsafe_identity"]:
                values = {
                    "reward": None,
                    "integrity": None,
                    "reward_gated": None,
                    "stop_reason": None,
                    "copy_verdict": None,
                }
                if row["unsafe_identity"]:
                    error: str | None = "unsafe native identity; no projection attempted"
                else:
                    error = "unknown native identity; no projection attempted"
            else:
                values, trusted, problems, notes = _resolve_projections(
                    row=row, job_id=job_id, trial_id=trial_id,
                    hot=hot, target_root=target_root, cold=cold,
                )
                source_notes.update(notes)
                if problems and read_only:
                    error = _read_only_projection(row, values, trusted)
                    if error is None:
                        in_memory.append(f"{job_id}/{trial_id}")
                    else:
                        error = "; ".join([*problems, error])
                elif problems:
                    backfill_error = _backfill_job(
                        repo_root=repo,
                        job_dir=job_dir,
                        target_root=target_root,
                        cache=backfill_cache,
                    )
                    if backfill_error is None:
                        backfilled.append(f"{job_id}/{trial_id}")
                        values, trusted, problems, rereread = _resolve_projections(
                            row=row,
                            job_id=job_id,
                            trial_id=trial_id,
                            hot=hot,
                            target_root=target_root,
                            cold=cold,
                        )
                        source_notes.update(rereread)
                        error = "; ".join(problems) if problems else None
                    else:
                        _null_untrusted(values, trusted)
                        error = "; ".join([*problems, backfill_error])
                else:
                    error = None
            cut_short = cut_short_by_our_limits(values["stop_reason"])
            result = row.get("result") or {}
            trial_name = result.get("trial_name")
            task_name = result.get("task_name")
            record = {
                "job_id": job_id,
                "trial_id": trial_id,
                "campaign": posture.get("campaign"),
                "job": job_dir.name,
                "trial": str(trial_name) if trial_name is not None else trial_dir.name,
                "task": str(task_name) if task_name is not None else None,
                "date": posture.get("date"),
                "model": posture.get("model"),
                "harness": posture.get("harness"),
                "egress_lock": posture.get("egress_lock"),
                "reference_profile": posture.get("reference_profile"),
                "reference_profile_match": posture.get("reference_profile_match"),
                "reference_profile_diffs": posture.get("reference_profile_diffs"),
                "stop_reason": values["stop_reason"],
                "cut_short_by_our_limits": cut_short,
                "infra": posture.get("infra"),
                "infra_exception": posture.get("infra_exception"),
                "reward": values["reward"],
                "integrity": values["integrity"],
                "reward_gated": values["reward_gated"],
                "copy_verdict": values["copy_verdict"],
                "laminar_trace_id": posture.get("laminar_trace_id"),
                "source_job_dir": str(job_dir),
                "source_trial_dir": str(trial_dir),
                "projection_error": error,
            }
            if error is not None:
                projection_errors[str(trial_dir)] = error
            records.append(record)
        schema = pa.schema(
            [
                ("job_id", pa.string()),
                ("trial_id", pa.string()),
                ("campaign", pa.string()),
                ("job", pa.string()),
                ("trial", pa.string()),
                ("task", pa.string()),
                ("date", pa.string()),
                ("model", pa.string()),
                ("harness", pa.string()),
                ("egress_lock", pa.bool_()),
                ("reference_profile", pa.string()),
                ("reference_profile_match", pa.bool_()),
                ("reference_profile_diffs", pa.string()),
                ("stop_reason", pa.string()),
                ("cut_short_by_our_limits", pa.bool_()),
                ("infra", pa.bool_()),
                ("infra_exception", pa.string()),
                ("reward", pa.float64()),
                ("integrity", pa.float64()),
                ("reward_gated", pa.float64()),
                ("copy_verdict", pa.string()),
                ("laminar_trace_id", pa.string()),
                ("source_job_dir", pa.string()),
                ("source_trial_dir", pa.string()),
                ("projection_error", pa.string()),
            ]
        )
        table = pa.Table.from_pylist(records, schema=schema)
        con.register("trials_base", table)
        con.execute(_TRIALS_SQL)
    except Exception:
        con.close()
        raise
    info: dict[str, Any] = {
        "repo_root": str(repo),
        "roots": [str(Path(p)) for p in scan_roots],
        "derived_roots": [str(Path(p)) for p in ordered_roots],
        "target_derived_root": str(target_root),
        "n_inventory": len(inventory),
        "n_rows": len(rows),
        "n_backfilled": len(backfilled),
        "backfilled": backfilled,
        "read_only": read_only,
        "task_names": list(task_names) if task_names is not None else None,
        "in_memory_projections": in_memory,
        "unreadable_paths": unreadable,
        "projection_errors": projection_errors,
        "projection_source_errors": sorted([*lake_errors, *source_notes]),
        "arrow_table": table,
    }
    return con, info


def _format_value(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _print_summary(con: duckdb.DuckDBPyConnection) -> int:
    total = con.execute("SELECT COUNT(*) FROM trials").fetchall()[0][0]
    legit = con.execute("SELECT COUNT(*) FROM trials WHERE legit").fetchall()[0][0]
    print(f"total: {total}")
    print(f"legit: {legit}")
    rows = con.execute(
        "SELECT COALESCE(\"campaign\", 'NULL'), COUNT(*), "
        "SUM(CASE WHEN \"legit\" THEN 1 ELSE 0 END) "
        'FROM trials GROUP BY 1 ORDER BY 1 NULLS LAST'
    ).fetchall()
    for campaign, count, legit_count in rows:
        print(f"campaign {campaign}: {count} (legit {legit_count or 0})")
    return 0


def _run_sql(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    try:
        statements = con.extract_statements(sql)
    except Exception as exc:  # noqa: BLE001 -- syntax errors are usage errors
        print(f"trials: bad sql: {exc}", file=sys.stderr)
        return 1
    if not statements:
        print("trials: empty sql", file=sys.stderr)
        return 1
    for statement in statements:
        if statement.type != duckdb.StatementType.SELECT:
            print(
                f"trials: refusing non-SELECT statement ({statement.type})",
                file=sys.stderr,
            )
            return 1
    for statement in statements:
        try:
            cursor = con.execute(statement.query)
            columns = [d[0] for d in cursor.description]
            fetched = cursor.fetchall()
        except Exception as exc:  # noqa: BLE001 -- query errors are usage errors
            print(f"trials: query failed: {exc}", file=sys.stderr)
            return 1
        print("\t".join(columns))
        for values in fetched:
            print("\t".join(_format_value(v) for v in values))
    return 0


def build_trials_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``evallab trials`` subcommand (one self-contained block)."""
    parser = subparsers.add_parser(
        "trials",
        help="Local trial census over runs roots (transient DuckDB view, no store)",
    )
    parser.add_argument(
        "--runs-dir",
        action="append",
        default=[],
        dest="runs_dir",
        help="Explicit runs/job root (repeatable); default scans main/worktree roots",
    )
    parser.add_argument(
        "--derived-root",
        default=None,
        help="Explicit derived parquet root for projection reads",
    )
    parser.add_argument(
        "--sql",
        default=None,
        help="Ad-hoc read-only SELECT over the trials view",
    )
    parser.set_defaults(func=command)


def command(args: argparse.Namespace, root: Path, *, harbor: Any | None = None) -> int:
    """Run ``evallab trials``: campaign/legit counts plus total, or ``--sql`` rows."""
    del harbor
    repo_root = Path(root).resolve()
    explicit_roots = [_resolve(repo_root, Path(p)) for p in (args.runs_dir or [])]
    explicit_derived = (
        _resolve(repo_root, Path(args.derived_root))
        if getattr(args, "derived_root", None)
        else None
    )
    try:
        con, info = connect_trials(
            repo_root=repo_root,
            roots=explicit_roots or None,
            derived_root=explicit_derived,
        )
    except Exception as exc:  # noqa: BLE001 -- census failures are exit codes
        print(f"trials: error: {exc}", file=sys.stderr)
        return 1
    try:
        if getattr(args, "sql", None) is not None:
            status = _run_sql(con, args.sql)
        else:
            status = _print_summary(con)
        n_errors = len(info["projection_errors"])
        unreadable = info["unreadable_paths"]
        if n_errors or unreadable:
            print(
                f"trials: {n_errors}/{info['n_rows']} rows carry projection errors; "
                f"{len(unreadable)} unreadable result.json files",
                file=sys.stderr,
            )
            for path in unreadable:
                print(f"trials: unreadable result.json: {path}", file=sys.stderr)
        return status
    finally:
        con.close()
