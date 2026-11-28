from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ._bespoke_adapter import BESPOKE_BOARD_PATHS, BESPOKE_BUNDLE_PATHS
from ._bespoke_board_parser import parse_bespoke_board_event_dicts
from ._git_bundle import GitCommitRecord, is_seed_author, read_git_bundle
from ._multiagent_io import (
    as_str,
    epoch_to_iso_utc,
    read_jsonl_records,
    safe_regular_file,
)
from ._olam_world_parsers import parse_crucible_rows, parse_muster_world_rows

EventKind = Literal[
    "message_post",
    "message_fetch",
    "world_read",
    "world_action",
    "artifact_write",
    "lifecycle",
    "auth_violation",
    "policy_refusal",
    "probe",
    "world_internal",
]
EventVisibility = Literal[
    "public",
    "direct",
    "multi",
    "world_audit",
    "world_private",
    "lifecycle",
]
EventPhase = Literal["setup", "play", "post_play", "verification", "lifecycle", "unknown"]
EventSourceFormat = Literal[
    "olam_muster_world",
    "harbor_crucible_audit",
    "harbor_trial_events",
    "bespoke_board_v2",
    "bespoke_board_legacy",
    "bespoke_public_board",
    "bespoke_git_bundle",
]

_WORLD_LOG_CANDIDATES: tuple[tuple[str, ...], ...] = (
    ("verifier", "events.jsonl"),
    ("verifier", "audit.jsonl"),
    ("artifacts", "logs", "world", "events.jsonl"),
    ("artifacts", "logs", "world", "audit.jsonl"),
    ("artifacts", "world", "events.jsonl"),
    ("artifacts", "world", "audit.jsonl"),
    ("world", "events.jsonl"),
    ("world", "audit.jsonl"),
    ("audit.jsonl",),
)
_SYSTEM_ACTORS = frozenset({"world", "system", "@harness", "harbor"})


class NormalizedWorldEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)
    ts: str | None = None
    actor: str | None = None
    kind: EventKind
    target: str | None = None
    msg_id: str | None = None
    recipients: list[str] = Field(default_factory=list)
    visibility: EventVisibility
    accepted: bool = True
    phase: EventPhase = "unknown"
    source_format: EventSourceFormat

    ts_epoch: float | None = None
    raw_kind: str = ""
    fetched_msg_ids: list[str] = Field(default_factory=list)
    text: str | None = None
    action_signature: str | None = None
    request_id: str | None = None
    authenticated: bool | None = None
    claimed_actor: str | None = None
    files: list[str] = Field(default_factory=list)
    branch: str | None = None
    commit_sha: str | None = None
    overwrites_actor: str | None = None
    status_code: int | None = None
    late: bool = False
    detail: dict[str, Any] = Field(default_factory=dict)


class WorldEventLog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_log_present: bool = False
    trial_events_present: bool = False
    git_bundle_present: bool = False
    event_log_paths: list[str] = Field(default_factory=list)
    source_formats: list[str] = Field(default_factory=list)
    events: list[NormalizedWorldEvent] = Field(default_factory=list)
    lifecycle_events: list[NormalizedWorldEvent] = Field(default_factory=list)
    git_events: list[NormalizedWorldEvent] = Field(default_factory=list)
    git_commits: list[GitCommitRecord] = Field(default_factory=list)
    git_branches: list[str] = Field(default_factory=list)
    git_shared_branches: dict[str, list[str]] = Field(default_factory=dict)
    git_shared_files: dict[str, list[str]] = Field(default_factory=dict)
    end_of_play_seq: int | None = None
    end_of_play_ts: str | None = None
    end_of_play_ts_epoch: float | None = None
    end_of_play_source: str | None = None
    parse_errors: list[str] = Field(default_factory=list)


def _parse_bespoke_board_rows(
    rows: list[dict[str, Any]],
    *,
    member_aliases: dict[str, str] | None = None,
) -> list[NormalizedWorldEvent]:
    return [
        NormalizedWorldEvent.model_validate(item)
        for item in parse_bespoke_board_event_dicts(rows, member_aliases=member_aliases)
    ]


def _parse_trial_events(
    path: Path,
) -> tuple[list[NormalizedWorldEvent], str | None, float | None, int]:
    rows, errs = read_jsonl_records(path)
    events: list[NormalizedWorldEvent] = []
    agent_end_ts: str | None = None
    agent_end_epoch: float | None = None
    phase_map: dict[str, EventPhase] = {
        "start": "setup",
        "environment-start": "setup",
        "agent-start": "play",
        "agent-end": "lifecycle",
        "verification-start": "verification",
        "end": "lifecycle",
        "cancel": "lifecycle",
    }
    for idx, rec in enumerate(rows, start=1):
        ev_name = as_str(rec.get("event")) or "unknown"
        raw_ts = rec.get("timestamp") if rec.get("timestamp") is not None else rec.get("ts")
        ts_iso, ts_epoch = epoch_to_iso_utc(raw_ts)
        if ev_name == "agent-end" and ts_epoch is not None:
            agent_end_ts, agent_end_epoch = ts_iso, ts_epoch
        events.append(
            NormalizedWorldEvent(
                seq=idx,
                ts=ts_iso,
                ts_epoch=ts_epoch,
                actor="harbor",
                kind="lifecycle",
                target=ev_name,
                visibility="lifecycle",
                accepted=True,
                phase=phase_map.get(ev_name, "lifecycle"),
                source_format="harbor_trial_events",
                raw_kind=ev_name,
                authenticated=True,
            )
        )
    return events, agent_end_ts, agent_end_epoch, errs


def _load_world_and_board_events(
    root: Path,
    member_aliases: dict[str, str] | None,
    event_log_paths: list[str],
    parse_errors: list[str],
) -> tuple[list[NormalizedWorldEvent], bool, int | None, str | None, float | None, str | None]:
    events: list[NormalizedWorldEvent] = []
    close_seq: int | None = None
    close_ts: str | None = None
    close_epoch: float | None = None
    end_source: str | None = None

    world_path = next(
        (p for parts in _WORLD_LOG_CANDIDATES if (p := safe_regular_file(root, *parts))), None
    )
    if world_path is not None:
        event_log_paths.append(world_path.relative_to(root).as_posix())
        rows, errs = read_jsonl_records(world_path)
        if errs:
            parse_errors.append(f"{world_path.relative_to(root).as_posix()}:invalid_lines={errs}")
        if rows:
            if any("event_type" in r for r in rows[:20]):
                raw_events = parse_crucible_rows(rows)
            else:
                rel_w = world_path.relative_to(root).as_posix()
                bad_seq = any(
                    type(r.get("seq")) is not int or r.get("seq") != idx or not as_str(r.get("kind"))
                    for idx, r in enumerate(rows, start=1)
                )
                if bad_seq:
                    parse_errors.append(f"{rel_w}:invalid_seq")
                raw_events, close_seq, close_ts, close_epoch = parse_muster_world_rows(rows)
                if errs > 0:
                    for item in raw_events:
                        if "hash_chain_valid" in item["detail"]:
                            item["detail"]["hash_chain_valid"] = False
                if any(item["detail"].get("hash_chain_valid") is False for item in raw_events):
                    parse_errors.append(f"{rel_w}:broken_hash_chain")
                if close_seq is not None:
                    end_source = "admin.close"
            events.extend(NormalizedWorldEvent.model_validate(item) for item in raw_events)

    board_path = next(
        (p for parts in BESPOKE_BOARD_PATHS if (p := safe_regular_file(root, *parts))), None
    )
    if board_path is not None:
        event_log_paths.append(board_path.relative_to(root).as_posix())
        rows, errs = read_jsonl_records(board_path)
        if errs:
            parse_errors.append(f"{board_path.relative_to(root).as_posix()}:invalid_lines={errs}")
        if rows:
            events.extend(_parse_bespoke_board_rows(rows, member_aliases=member_aliases))

    return (
        events,
        (world_path is not None or board_path is not None),
        close_seq,
        close_ts,
        close_epoch,
        end_source,
    )


def _resolve_git_claimed_actor(
    commit: GitCommitRecord,
    roster_set: set[str],
    exact_canon: dict[str, str],
    alias_canon: dict[str, str],
) -> str | None:
    if not commit.author_email_mismatch:
        return None
    claimed_email_user = commit.author_email.split("@", 1)[0].strip() or None
    resolved = commit.resolved_actor or ""
    for raw_cand in (claimed_email_user, commit.author_name):
        if not raw_cand:
            continue
        cand = exact_canon.get(raw_cand) or alias_canon.get(raw_cand.lower(), raw_cand)
        if cand != resolved and (cand in roster_set or cand.lower() != resolved.lower()):
            return cand
    return None


def _git_commit_to_event(
    idx: int,
    commit: GitCommitRecord,
    non_trunk: set[str],
    default_branch: str,
    roster_set: set[str],
    exact_canon: dict[str, str],
    alias_canon: dict[str, str],
) -> NormalizedWorldEvent:
    branch = next((b for b in commit.branches if b in non_trunk), None) or (
        commit.branches[0] if commit.branches else default_branch
    )
    seed = commit.resolved_actor is None and is_seed_author(commit.author_name, commit.author_email)
    return NormalizedWorldEvent(
        seq=idx,
        ts=commit.ts,
        ts_epoch=commit.ts_epoch,
        actor=commit.resolved_actor or commit.author_name,
        kind="artifact_write",
        target=commit.files[0] if len(commit.files) == 1 else branch,
        visibility="world_audit",
        accepted=True,
        phase="play",
        source_format="bespoke_git_bundle",
        raw_kind="git_commit",
        text=commit.subject,
        action_signature=f"git_commit:{branch}:{','.join(commit.files)}",
        authenticated=bool(commit.resolved_actor is not None or seed),
        claimed_actor=_resolve_git_claimed_actor(commit, roster_set, exact_canon, alias_canon),
        files=commit.files,
        branch=branch,
        commit_sha=commit.sha,
        detail={"author_email_mismatch": commit.author_email_mismatch, "is_seed": seed},
    )


def _load_git_bundle_events(
    root: Path,
    roster: list[str] | None,
    member_aliases: dict[str, str] | None,
    parse_errors: list[str],
) -> tuple[
    bool,
    list[NormalizedWorldEvent],
    list[GitCommitRecord],
    list[str],
    dict[str, list[str]],
    dict[str, list[str]],
]:
    bundle_path = next(
        (p for parts in BESPOKE_BUNDLE_PATHS if (p := safe_regular_file(root, *parts))), None
    )
    if bundle_path is None:
        return False, [], [], [], {}, {}
    summary = read_git_bundle(bundle_path, roster or [], member_aliases=member_aliases)
    parse_errors.extend(summary.errors)
    non_trunk = {b for b in summary.branches if b not in (summary.default_branch, "main", "master")}
    roster_set = set(roster or ())
    exact_canon = {name: name for name in sorted(roster_set)}
    alias_canon = {name.lower(): name for name in sorted(roster_set)}
    for k, v in sorted((member_aliases or {}).items()):
        if v in roster_set or not roster_set:
            exact_canon.setdefault(k, v)
            alias_canon.setdefault(k.lower(), v)
    git_events = [
        _git_commit_to_event(
            idx, commit, non_trunk, summary.default_branch, roster_set, exact_canon, alias_canon
        )
        for idx, commit in enumerate(summary.commits, start=1)
        if not commit.is_root_commit
    ]
    return (
        True,
        git_events,
        summary.commits,
        summary.branches,
        summary.shared_branches,
        summary.shared_files,
    )


def _apply_post_play_cutoff(
    events: list[NormalizedWorldEvent],
    git_events: list[NormalizedWorldEvent],
    close_epoch: float | None,
    end_source: str | None,
) -> None:
    if close_epoch is None:
        return
    grace_sec = 5.0
    for ev in (*events, *git_events):
        if (
            ev.phase != "post_play"
            and (end_source != "admin.close" or ev.source_format != "olam_muster_world")
            and ev.kind not in ("lifecycle", "world_internal")
            and ev.actor not in _SYSTEM_ACTORS
            and ev.ts_epoch is not None
            and ev.ts_epoch > close_epoch + grace_sec
        ):
            ev.phase = "post_play"


def load_world_events(
    trial_dir: Path,
    *,
    roster: list[str] | None = None,
    member_aliases: dict[str, str] | None = None,
    agent_execution_finished_at: str | None = None,
) -> WorldEventLog:
    root = trial_dir.resolve()
    parse_errors: list[str] = []
    event_log_paths: list[str] = []
    events, event_log_present, close_seq, close_ts, close_epoch, end_source = (
        _load_world_and_board_events(root, member_aliases, event_log_paths, parse_errors)
    )
    lifecycle_events: list[NormalizedWorldEvent] = []
    te_path = safe_regular_file(root, "trial-events.jsonl")
    if te_path is not None:
        lifecycle_events, te_end_ts, te_end_epoch, errs = _parse_trial_events(te_path)
        if errs:
            parse_errors.append(f"trial-events.jsonl:invalid_lines={errs}")
        if end_source is None and te_end_epoch is not None:
            close_ts, close_epoch, end_source = te_end_ts, te_end_epoch, "trial_events.agent_end"
    if end_source is None and agent_execution_finished_at is not None:
        fb_ts, fb_epoch = epoch_to_iso_utc(agent_execution_finished_at)
        if fb_epoch is not None:
            close_ts, close_epoch = fb_ts, fb_epoch
            end_source = "result.agent_execution.finished_at"
    (
        git_bundle_present,
        git_events,
        git_commits,
        git_branches,
        git_shared_branches,
        git_shared_files,
    ) = _load_git_bundle_events(root, roster, member_aliases, parse_errors)
    _apply_post_play_cutoff(events, git_events, close_epoch, end_source)
    formats = sorted({ev.source_format for ev in (*events, *lifecycle_events, *git_events)})
    return WorldEventLog(
        event_log_present=event_log_present,
        trial_events_present=te_path is not None,
        git_bundle_present=git_bundle_present,
        event_log_paths=event_log_paths,
        source_formats=formats,
        events=events,
        lifecycle_events=lifecycle_events,
        git_events=git_events,
        git_commits=git_commits,
        git_branches=git_branches,
        git_shared_branches=git_shared_branches,
        git_shared_files=git_shared_files,
        end_of_play_seq=close_seq,
        end_of_play_ts=close_ts,
        end_of_play_ts_epoch=close_epoch,
        end_of_play_source=end_source,
        parse_errors=parse_errors,
    )
