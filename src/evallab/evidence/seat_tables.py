from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict

from ._multiagent_io import safe_finite_float, sanitize_json_value
from .multiagent_adapter import discover_trial_dirs, read_multiagent_trial
from .seat_forensics import MemberForensicsReport, TrialForensicsReport, analyze_normalized_trial
from .world_events import NormalizedWorldEvent, WorldEventLog, load_world_events

ExportFormat = Literal["parquet", "jsonl"]
TABLE_NAMES = ("trials", "seats", "events", "causal_links", "failure_episodes")
_INT64_MIN = -(1 << 63)
_INT64_MAX = (1 << 63) - 1

TRIALS_SCHEMA = pa.schema(
    [
        ("trial_path", pa.string()),
        ("trial_name", pa.string()),
        ("trial_id", pa.string()),
        ("task_name", pa.string()),
        ("ecosystem", pa.string()),
        ("orchestration", pa.string()),
        ("board_policy", pa.string()),
        ("comms_disabled", pa.bool_()),
        ("primary_reward", pa.float64()),
        ("rewards_json", pa.string()),
        ("reward_valid", pa.bool_()),
        ("trial_exception_type", pa.string()),
        ("n_members", pa.int64()),
        ("total_input_tokens", pa.int64()),
        ("total_cache_tokens", pa.int64()),
        ("total_output_tokens", pa.int64()),
        ("total_cost_usd", pa.float64()),
        ("known_cost_usd", pa.float64()),
        ("n_world_events", pa.int64()),
        ("n_lifecycle_events", pa.int64()),
        ("n_git_commits", pa.int64()),
        ("causal_links_count", pa.int64()),
        ("messages_posted", pa.int64()),
        ("messages_fetched", pa.int64()),
        ("messages_in_context", pa.int64()),
        ("messages_acted_on", pa.int64()),
        ("unread_at_action", pa.int64()),
        ("never_fetched", pa.int64()),
        ("duplicate_target_actions", pa.int64()),
        ("stale_read_write_races", pa.int64()),
        ("shared_branch_or_file_conflicts", pa.int64()),
        ("post_end_of_play_events", pa.int64()),
        ("logs_complete", pa.bool_()),
        ("cost_complete", pa.bool_()),
        ("seat_error_with_reward", pa.bool_()),
        ("unauthenticated_author_mismatch", pa.bool_()),
        ("event_log_present", pa.bool_()),
        ("failure_episodes_count", pa.int64()),
    ]
)

SEATS_SCHEMA = pa.schema(
    [
        ("trial_path", pa.string()),
        ("trial_name", pa.string()),
        ("trial_id", pa.string()),
        ("ecosystem", pa.string()),
        ("member_id", pa.string()),
        ("display_name", pa.string()),
        ("role", pa.string()),
        ("under_test", pa.bool_()),
        ("agent_name", pa.string()),
        ("harness", pa.string()),
        ("model_name", pa.string()),
        ("model_provider", pa.string()),
        ("ran_model", pa.bool_()),
        ("trajectory_status", pa.string()),
        ("n_steps", pa.int64()),
        ("n_tool_calls", pa.int64()),
        ("n_actions", pa.int64()),
        ("n_reads", pa.int64()),
        ("n_refused", pa.int64()),
        ("n_probes", pa.int64()),
        ("exception_type", pa.string()),
        ("seat_reward", pa.float64()),
        ("cost_usd", pa.float64()),
        ("cost_status", pa.string()),
        ("logs_complete", pa.bool_()),
        ("cost_complete", pa.bool_()),
        ("unauthenticated_author_mismatch", pa.bool_()),
        ("linked_step_events", pa.int64()),
        ("messages_posted", pa.int64()),
        ("messages_fetched", pa.int64()),
        ("messages_in_context", pa.int64()),
        ("messages_acted_on", pa.int64()),
        ("unread_at_action", pa.int64()),
        ("never_fetched", pa.int64()),
        ("duplicate_target_actions", pa.int64()),
        ("stale_read_write_races", pa.int64()),
        ("shared_branch_or_file_conflicts", pa.int64()),
        ("post_end_of_play_events", pa.int64()),
    ]
)

EVENTS_SCHEMA = pa.schema(
    [
        ("trial_path", pa.string()),
        ("trial_name", pa.string()),
        ("trial_id", pa.string()),
        ("ecosystem", pa.string()),
        ("source_format", pa.string()),
        ("seq", pa.int64()),
        ("ts", pa.string()),
        ("ts_epoch", pa.float64()),
        ("actor", pa.string()),
        ("kind", pa.string()),
        ("raw_kind", pa.string()),
        ("target", pa.string()),
        ("msg_id", pa.string()),
        ("recipients_json", pa.string()),
        ("fetched_msg_ids_json", pa.string()),
        ("visibility", pa.string()),
        ("accepted", pa.bool_()),
        ("phase", pa.string()),
        ("authenticated", pa.bool_()),
        ("claimed_actor", pa.string()),
        ("branch", pa.string()),
        ("commit_sha", pa.string()),
        ("status_code", pa.int64()),
        ("late", pa.bool_()),
        ("text", pa.string()),
    ]
)

CAUSAL_LINKS_SCHEMA = pa.schema(
    [
        ("trial_path", pa.string()),
        ("trial_name", pa.string()),
        ("trial_id", pa.string()),
        ("member_id", pa.string()),
        ("step_id", pa.int64()),
        ("event_source", pa.string()),
        ("event_seq", pa.int64()),
        ("event_kind", pa.string()),
        ("match_basis", pa.string()),
        ("evidence", pa.string()),
    ]
)

FAILURE_EPISODES_SCHEMA = pa.schema(
    [
        ("trial_path", pa.string()),
        ("trial_name", pa.string()),
        ("trial_id", pa.string()),
        ("ecosystem", pa.string()),
        ("episode_id", pa.string()),
        ("kind", pa.string()),
        ("severity", pa.string()),
        ("actor", pa.string()),
        ("step_id", pa.int64()),
        ("event_seq", pa.int64()),
        ("ts", pa.string()),
        ("source_format", pa.string()),
        ("target", pa.string()),
        ("peer_actor", pa.string()),
        ("peer_event_seq", pa.int64()),
        ("msg_id", pa.string()),
        ("summary", pa.string()),
        ("evidence_json", pa.string()),
    ]
)

SCHEMAS: dict[str, pa.Schema] = {
    "trials": TRIALS_SCHEMA,
    "seats": SEATS_SCHEMA,
    "events": EVENTS_SCHEMA,
    "causal_links": CAUSAL_LINKS_SCHEMA,
    "failure_episodes": FAILURE_EPISODES_SCHEMA,
}


class SeatTablesExportSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "evallab.seat-tables-export/v1"
    out_dir: str
    format: ExportFormat
    n_trials: int
    row_counts: dict[str, int]
    files: dict[str, str]
    sha256_by_table: dict[str, str]


def _primary_reward(rewards: dict[str, float] | None) -> float | None:
    if not rewards:
        return None
    if "reward" in rewards:
        return rewards["reward"]
    first_key = sorted(rewards)[0]
    return rewards[first_key]


def _dump_compact(value: object) -> str:
    return json.dumps(
        sanitize_json_value(value),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _clamp_int64(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value > _INT64_MAX:
            return _INT64_MAX
        if value < _INT64_MIN:
            return _INT64_MIN
        return value
    return None


def _coerce_column(field: pa.Field, table_rows: list[dict[str, Any]]) -> list[Any]:
    name = field.name
    if pa.types.is_int64(field.type):
        return [_clamp_int64(r.get(name)) for r in table_rows]
    if pa.types.is_floating(field.type):
        return [safe_finite_float(r.get(name)) for r in table_rows]
    if pa.types.is_string(field.type):
        return [
            sanitize_json_value(val) if isinstance(val, str) else val
            for val in (r.get(name) for r in table_rows)
        ]
    return [r.get(name) for r in table_rows]


def _trial_table_row(rel_path: str, rep: TrialForensicsReport) -> dict[str, Any]:
    f, c, ig = rep.funnel, rep.collisions, rep.integrity
    return {
        "trial_path": rel_path,
        "trial_name": rep.trial_name,
        "trial_id": rep.trial_id,
        "task_name": rep.task_name,
        "ecosystem": rep.ecosystem,
        "orchestration": rep.orchestration,
        "board_policy": rep.board_policy,
        "comms_disabled": rep.comms_disabled,
        "primary_reward": _primary_reward(rep.rewards),
        "rewards_json": _dump_compact(rep.rewards) if rep.rewards is not None else None,
        "reward_valid": rep.reward_valid,
        "trial_exception_type": rep.trial_exception_type,
        "n_members": rep.n_members,
        "total_input_tokens": rep.total_input_tokens,
        "total_cache_tokens": rep.total_cache_tokens,
        "total_output_tokens": rep.total_output_tokens,
        "total_cost_usd": rep.total_cost_usd,
        "known_cost_usd": rep.known_cost_usd,
        "n_world_events": rep.n_world_events,
        "n_lifecycle_events": rep.n_lifecycle_events,
        "n_git_commits": rep.n_git_commits,
        "causal_links_count": rep.causal_links_count,
        "messages_posted": f.messages_posted,
        "messages_fetched": f.messages_fetched,
        "messages_in_context": f.messages_in_context,
        "messages_acted_on": f.messages_acted_on,
        "unread_at_action": f.unread_at_action,
        "never_fetched": f.never_fetched,
        "duplicate_target_actions": c.duplicate_target_actions,
        "stale_read_write_races": c.stale_read_write_races,
        "shared_branch_or_file_conflicts": c.shared_branch_or_file_conflicts,
        "post_end_of_play_events": c.post_end_of_play_events,
        "logs_complete": ig.logs_complete,
        "cost_complete": ig.cost_complete,
        "seat_error_with_reward": ig.seat_error_with_reward,
        "unauthenticated_author_mismatch": ig.unauthenticated_author_mismatch,
        "event_log_present": ig.event_log_present,
        "failure_episodes_count": len(rep.failure_episodes),
    }


def _seat_table_row(
    rel_path: str, rep: TrialForensicsReport, mid: str, m: MemberForensicsReport
) -> dict[str, Any]:
    mf, mc = m.funnel, m.collisions
    return {
        "trial_path": rel_path,
        "trial_name": rep.trial_name,
        "trial_id": rep.trial_id,
        "ecosystem": rep.ecosystem,
        "member_id": mid,
        "display_name": m.display_name,
        "role": m.role,
        "under_test": m.under_test,
        "agent_name": m.agent_name,
        "harness": m.harness,
        "model_name": m.model_name,
        "model_provider": m.model_provider,
        "ran_model": m.ran_model,
        "trajectory_status": m.trajectory_status,
        "n_steps": m.n_steps,
        "n_tool_calls": m.n_tool_calls,
        "n_actions": m.n_actions,
        "n_reads": m.n_reads,
        "n_refused": m.n_refused,
        "n_probes": m.n_probes,
        "exception_type": m.exception_type,
        "seat_reward": m.seat_reward,
        "cost_usd": m.cost_usd,
        "cost_status": m.cost_status,
        "logs_complete": m.logs_complete,
        "cost_complete": m.cost_complete,
        "unauthenticated_author_mismatch": m.unauthenticated_author_mismatch,
        "linked_step_events": m.linked_step_events,
        "messages_posted": mf.messages_posted,
        "messages_fetched": mf.messages_fetched,
        "messages_in_context": mf.messages_in_context,
        "messages_acted_on": mf.messages_acted_on,
        "unread_at_action": mf.unread_at_action,
        "never_fetched": mf.never_fetched,
        "duplicate_target_actions": mc.duplicate_target_actions,
        "stale_read_write_races": mc.stale_read_write_races,
        "shared_branch_or_file_conflicts": mc.shared_branch_or_file_conflicts,
        "post_end_of_play_events": mc.post_end_of_play_events,
    }


def _event_table_row(
    rel_path: str, rep: TrialForensicsReport, ev: NormalizedWorldEvent
) -> dict[str, Any]:
    return {
        "trial_path": rel_path,
        "trial_name": rep.trial_name,
        "trial_id": rep.trial_id,
        "ecosystem": rep.ecosystem,
        "source_format": ev.source_format,
        "seq": ev.seq,
        "ts": ev.ts,
        "ts_epoch": ev.ts_epoch,
        "actor": ev.actor,
        "kind": ev.kind,
        "raw_kind": ev.raw_kind,
        "target": ev.target,
        "msg_id": ev.msg_id,
        "recipients_json": _dump_compact(ev.recipients),
        "fetched_msg_ids_json": _dump_compact(ev.fetched_msg_ids),
        "visibility": ev.visibility,
        "accepted": ev.accepted,
        "phase": ev.phase,
        "authenticated": ev.authenticated,
        "claimed_actor": ev.claimed_actor,
        "branch": ev.branch,
        "commit_sha": ev.commit_sha,
        "status_code": ev.status_code,
        "late": ev.late,
        "text": ev.text,
    }


def _append_trial_rows(
    rows: dict[str, list[dict[str, Any]]],
    rel_path: str,
    wlog: WorldEventLog,
    rep: TrialForensicsReport,
) -> None:
    rows["trials"].append(_trial_table_row(rel_path, rep))
    for mid, m in sorted(rep.members.items()):
        rows["seats"].append(_seat_table_row(rel_path, rep, mid, m))
    for ev in (*wlog.events, *wlog.lifecycle_events, *wlog.git_events):
        rows["events"].append(_event_table_row(rel_path, rep, ev))
    for link in rep.causal_links:
        rows["causal_links"].append(
            {
                "trial_path": rel_path,
                "trial_name": rep.trial_name,
                "trial_id": rep.trial_id,
                "member_id": link.member_id,
                "step_id": link.step_id,
                "event_source": link.event_source,
                "event_seq": link.event_seq,
                "event_kind": link.event_kind,
                "match_basis": link.match_basis,
                "evidence": link.evidence,
            }
        )
    for ep in rep.failure_episodes:
        rows["failure_episodes"].append(
            {
                "trial_path": rel_path,
                "trial_name": rep.trial_name,
                "trial_id": rep.trial_id,
                "ecosystem": rep.ecosystem,
                "episode_id": ep.episode_id,
                "kind": ep.kind,
                "severity": ep.severity,
                "actor": ep.actor,
                "step_id": ep.step_id,
                "event_seq": ep.event_seq,
                "ts": ep.ts,
                "source_format": ep.source_format,
                "target": ep.target,
                "peer_actor": ep.peer_actor,
                "peer_event_seq": ep.peer_event_seq,
                "msg_id": ep.msg_id,
                "summary": ep.summary,
                "evidence_json": _dump_compact(ep.evidence),
            }
        )


def collect_forensics_tables(path: Path) -> tuple[int, dict[str, list[dict[str, Any]]]]:
    root = path.expanduser().resolve()
    trial_dirs, _, _ = discover_trial_dirs(root)
    if not trial_dirs:
        raise ValueError(f"no Harbor trials found under {path}")

    rows: dict[str, list[dict[str, Any]]] = {name: [] for name in TABLE_NAMES}
    analyzed = []
    for td in trial_dirs:
        td_resolved = td.resolve()
        rel_path = (
            td_resolved.relative_to(root).as_posix()
            if td_resolved != root and root in td_resolved.parents
            else td_resolved.name
        )
        trial = read_multiagent_trial(td)
        wlog = load_world_events(
            td,
            roster=[m.member_id for m in trial.members],
            member_aliases=trial.member_aliases,
            agent_execution_finished_at=trial.agent_execution_finished_at,
        )
        rep = analyze_normalized_trial(trial, wlog)
        analyzed.append((rep.trial_name, rep.trial_id or "", rel_path, wlog, rep))
    analyzed.sort(key=lambda item: (item[0], item[1], item[2]))
    for _, _, rel_path, wlog, rep in analyzed:
        _append_trial_rows(rows, rel_path, wlog, rep)
    return len(analyzed), rows


def export_forensics_tables(
    path: Path,
    out_dir: Path,
    *,
    fmt: ExportFormat = "parquet",
) -> SeatTablesExportSummary:
    if fmt not in ("parquet", "jsonl"):
        raise ValueError(f"unsupported table export format: {fmt!r}")
    n_trials, rows = collect_forensics_tables(path)
    dest_dir = out_dir.expanduser().resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    files: dict[str, str] = {}
    row_counts: dict[str, int] = {}
    digests: dict[str, str] = {}
    staged: list[tuple[Path, Path]] = []
    pid = os.getpid()

    try:
        for name in TABLE_NAMES:
            table_rows = rows[name]
            row_counts[name] = len(table_rows)
            out_file = dest_dir / f"{name}.{fmt}"
            tmp_file = dest_dir / f".{name}.{fmt}.{pid}.tmp"
            staged.append((tmp_file, out_file))
            if fmt == "jsonl":
                schema = SCHEMAS[name]
                int_fields = {f.name for f in schema if pa.types.is_int64(f.type)}
                clean_rows = [
                    {k: (_clamp_int64(v) if k in int_fields else v) for k, v in r.items()}
                    for r in table_rows
                ]
                payload = "".join(_dump_compact(r) + "\n" for r in clean_rows).encode("utf-8")
                tmp_file.write_bytes(payload)
            else:
                schema = SCHEMAS[name]
                cols = {field.name: _coerce_column(field, table_rows) for field in schema}
                pa_table = pa.table(cols, schema=schema)
                pq.write_table(
                    pa_table,
                    tmp_file,
                    compression="zstd",
                    use_dictionary=True,
                    write_statistics=True,
                )
            files[name] = out_file.as_posix()
            digests[name] = hashlib.sha256(tmp_file.read_bytes()).hexdigest()
        for tmp_file, out_file in staged:
            os.replace(tmp_file, out_file)
    finally:
        for tmp_file, _ in staged:
            if tmp_file.exists():
                tmp_file.unlink(missing_ok=True)

    return SeatTablesExportSummary(
        out_dir=dest_dir.as_posix(),
        format=fmt,
        n_trials=n_trials,
        row_counts=row_counts,
        files=files,
        sha256_by_table=digests,
    )
