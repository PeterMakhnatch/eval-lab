from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Any

from ._forensics_join import extract_timed_atif_file_reads
from ._forensics_metrics import actor_read_file_after
from ._forensics_rules import (
    happened_before,
    is_duplicate_eligible_action,
    normalize_repo_path,
    trunk_branches_for_log,
)
from ._forensics_sweep import log_group
from .multiagent_adapter import NormalizedMultiAgentTrial
from .world_events import NormalizedWorldEvent, WorldEventLog


def _prepare_collision_inputs(
    trial: NormalizedMultiAgentTrial,
    in_play_events: list[NormalizedWorldEvent],
    peer_posts: list[NormalizedWorldEvent],
    timed_reads: list[tuple[float, str, str]] | None,
) -> tuple[
    dict[tuple[str, str], list[NormalizedWorldEvent]],
    dict[tuple[str, str], list[float]],
    dict[str, list[NormalizedWorldEvent]],
]:
    file_reads_by_actor: dict[tuple[str, str], list[NormalizedWorldEvent]] = defaultdict(list)
    for ev in in_play_events:
        if ev.kind == "world_read" and ev.accepted and ev.actor is not None:
            for fpath in ev.files:
                file_reads_by_actor[(ev.actor, normalize_repo_path(fpath))].append(ev)
    atif_reads_by_actor: dict[tuple[str, str], list[float]] = defaultdict(list)
    reads_src = (
        timed_reads
        if timed_reads is not None
        else extract_timed_atif_file_reads(trial.member_steps)
    )
    for ts_ep, actor, fpath in reads_src:
        atif_reads_by_actor[(actor, normalize_repo_path(fpath))].append(ts_ep)
    posts_by_actor: dict[str, list[NormalizedWorldEvent]] = defaultdict(list)
    for post in peer_posts:
        if post.actor is not None and post.msg_id is not None:
            posts_by_actor[post.actor].append(post)
    return file_reads_by_actor, atif_reads_by_actor, posts_by_actor


def _eval_file_write_cleanliness(
    act: NormalizedWorldEvent,
    last_file_writer: dict[str, NormalizedWorldEvent],
    clean_file_owner: dict[str, str | None],
    file_reads_by_actor: dict[tuple[str, str], list[NormalizedWorldEvent]],
    atif_reads_by_actor: dict[tuple[str, str], list[float]],
) -> tuple[list[str], NormalizedWorldEvent | None, bool]:
    assert act.actor is not None
    colliding_files: list[str] = []
    peer_ev: NormalizedWorldEvent | None = None
    peer_touched = False
    any_self_clean = False
    all_clean = True
    for fpath in dict.fromkeys(normalize_repo_path(raw_f) for raw_f in act.files):
        prev = last_file_writer.get(fpath)
        if prev is None:
            file_clean = True
        elif prev.actor == act.actor:
            file_clean = clean_file_owner.get(fpath) == act.actor
            any_self_clean = any_self_clean or file_clean
        else:
            peer_touched = True
            file_clean = actor_read_file_after(
                act.actor, fpath, prev, act, file_reads_by_actor, atif_reads_by_actor
            )
            if not file_clean:
                colliding_files.append(fpath)
                peer_ev = prev
        if not file_clean:
            all_clean = False
        clean_file_owner[fpath] = act.actor if file_clean else None
        last_file_writer[fpath] = act
    coordinated = bool(act.files) and all_clean and (peer_touched or any_self_clean)
    return colliding_files, peer_ev, coordinated


def _eval_shared_file_and_branch_collision(
    act: NormalizedWorldEvent,
    step_id: int | None,
    world_log: WorldEventLog,
    shared_branches_s: set[str],
    allowed_branch_owners: dict[str, set[str]],
    trunk_branches: set[str],
    commit_branches: dict[str, list[str]],
    last_file_writer: dict[str, NormalizedWorldEvent],
    clean_file_owner: dict[str, str | None],
    first_branch_writer: dict[str, NormalizedWorldEvent],
    file_reads_by_actor: dict[tuple[str, str], list[NormalizedWorldEvent]],
    atif_reads_by_actor: dict[tuple[str, str], list[float]],
    make_ep: Callable[..., Any],
    episodes: list[Any],
) -> tuple[bool, bool]:
    assert act.actor is not None
    colliding_files, peer_ev, coordinated_handoff = _eval_file_write_cleanliness(
        act, last_file_writer, clean_file_owner, file_reads_by_actor, atif_reads_by_actor
    )
    branch_hit_name = _check_branch_collision(
        act, world_log, shared_branches_s, allowed_branch_owners,
        trunk_branches, commit_branches, first_branch_writer,
    )
    if branch_hit_name is not None and peer_ev is None:
        peer_ev = first_branch_writer.get(branch_hit_name)
    if not colliding_files and branch_hit_name is None:
        return False, coordinated_handoff
    target_lbl = colliding_files[0] if colliding_files else branch_hit_name
    episodes.append(
        make_ep(
            "destructive_collision",
            f"{act.actor} collided with {peer_ev.actor if peer_ev else 'peer'} on {target_lbl}",
            severity="error",
            actor=act.actor,
            step_id=step_id,
            event_seq=act.seq,
            ts=act.ts,
            source_format=act.source_format,
            target=target_lbl,
            peer_actor=peer_ev.actor if peer_ev else None,
            peer_event_seq=peer_ev.seq if peer_ev else None,
            suffix=f"src={act.source_format}",
            evidence={
                "collision_type": "shared_file_or_branch",
                "files": colliding_files,
                "branch": branch_hit_name,
            },
        )
    )
    return True, coordinated_handoff


def _check_branch_collision(
    act: NormalizedWorldEvent,
    world_log: WorldEventLog,
    shared_branches_s: set[str],
    allowed_branch_owners: dict[str, set[str]],
    trunk_branches: set[str],
    commit_branches: dict[str, list[str]],
    first_branch_writer: dict[str, NormalizedWorldEvent],
) -> str | None:
    has_commit_meta = bool(act.commit_sha and act.commit_sha in commit_branches)
    cand_branches = (
        commit_branches[act.commit_sha]
        if has_commit_meta and act.commit_sha is not None
        else ([act.branch] if act.branch else [])
    )
    if has_commit_meta and (trunk_branches & set(cand_branches)):
        cand_branches = []
    branch_hit_name: str | None = None
    for b in cand_branches:
        if b not in shared_branches_s:
            continue
        if world_log.git_commits and act.actor not in allowed_branch_owners.get(b, ()):
            continue
        prev_b = first_branch_writer.setdefault(b, act)
        if prev_b.actor != act.actor and branch_hit_name is None:
            branch_hit_name = b
    return branch_hit_name


def _handle_explicit_overwrite(
    act: NormalizedWorldEvent,
    step_id: int | None,
    clean_file_owner: dict[str, str | None],
    last_file_writer: dict[str, NormalizedWorldEvent],
    make_ep: Callable[..., Any],
    episodes: list[Any],
) -> None:
    for fpath in act.files:
        nf = normalize_repo_path(fpath)
        clean_file_owner[nf] = None
        last_file_writer[nf] = act
    episodes.append(
        make_ep(
            "destructive_collision",
            f"{act.actor} overwrote {act.overwrites_actor}'s artifact at {act.target}",
            severity="error",
            actor=act.actor,
            step_id=step_id,
            event_seq=act.seq,
            ts=act.ts,
            source_format=act.source_format,
            target=act.target,
            peer_actor=act.overwrites_actor,
            suffix="overwrite",
            evidence={"collision_type": "explicit_overwrite", "files": act.files},
        )
    )


def detect_duplicates_and_collisions(
    trial: NormalizedMultiAgentTrial,
    in_play_events: list[NormalizedWorldEvent],
    actions: list[NormalizedWorldEvent],
    peer_posts: list[NormalizedWorldEvent],
    first_fetch: dict[tuple[str, str, str], NormalizedWorldEvent],
    world_log: WorldEventLog,
    event_to_step: dict[tuple[str, int], int],
    comms_disabled: bool,
    make_ep: Callable[..., Any],
    *,
    timed_reads: list[tuple[float, str, str]] | None = None,
    shared_branches_set: set[str] | None = None,
) -> list[Any]:
    episodes: list[Any] = []
    first_by_sig: dict[str, NormalizedWorldEvent] = {}
    last_file_writer: dict[str, NormalizedWorldEvent] = {}
    clean_file_owner: dict[str, str | None] = {}
    first_branch_writer: dict[str, NormalizedWorldEvent] = {}
    shared_s = (
        shared_branches_set
        if shared_branches_set is not None
        else set(world_log.git_shared_branches)
    )
    allowed_owners = {b: set(owners) for b, owners in world_log.git_shared_branches.items()}
    trunk_branches = trunk_branches_for_log(world_log)
    commit_branches = {c.sha: c.branches for c in world_log.git_commits}
    file_reads_by_actor, atif_reads_by_actor, posts_by_actor = _prepare_collision_inputs(
        trial, in_play_events, peer_posts, timed_reads
    )
    for act in actions:
        if act.actor is None:
            continue
        step_id = event_to_step.get((act.source_format, act.seq))
        is_destructive, coordinated_handoff = False, False
        if act.kind == "artifact_write":
            if act.overwrites_actor and act.overwrites_actor != act.actor:
                is_destructive = True
                _handle_explicit_overwrite(
                    act, step_id, clean_file_owner, last_file_writer, make_ep, episodes
                )
            else:
                is_destructive, coordinated_handoff = _eval_shared_file_and_branch_collision(
                    act, step_id, world_log, shared_s, allowed_owners, trunk_branches,
                    commit_branches, last_file_writer, clean_file_owner, first_branch_writer,
                    file_reads_by_actor, atif_reads_by_actor, make_ep, episodes,
                )
        if not is_duplicate_eligible_action(act):
            continue
        sig = act.action_signature or f"{act.raw_kind}:{act.target or ''}"
        first = first_by_sig.setdefault(sig, act)
        if coordinated_handoff and not is_destructive:
            if first.actor != act.actor:
                first_by_sig[sig] = act
            continue
        if is_destructive or coordinated_handoff or first.actor == act.actor:
            continue
        episodes.append(
            _build_dup_work_ep(
                act, first, sig, step_id, comms_disabled, posts_by_actor, first_fetch, make_ep
            )
        )
    return episodes


def _build_dup_work_ep(
    act: NormalizedWorldEvent,
    first: NormalizedWorldEvent,
    sig: str,
    step_id: int | None,
    comms_disabled: bool,
    posts_by_actor: dict[str, list[NormalizedWorldEvent]],
    first_fetch: dict[tuple[str, str, str], NormalizedWorldEvent],
    make_ep: Callable[..., Any],
) -> Any:
    assert act.actor is not None and first.actor is not None
    informed = any(
        post.msg_id is not None
        and happened_before(first, post)
        and (f_ev := first_fetch.get((act.actor, log_group(post.source_format), post.msg_id)))
        is not None
        and happened_before(f_ev, act, inclusive=True)
        for post in posts_by_actor.get(first.actor, ())
    )
    return make_ep(
        "duplicate_target_work",
        f"{act.actor} repeated {first.actor}'s action on {act.target or sig}"
        + (" after reading peer report" if informed else ""),
        severity="info" if comms_disabled else "warn",
        actor=act.actor,
        step_id=step_id,
        event_seq=act.seq,
        ts=act.ts,
        source_format=act.source_format,
        target=act.target or sig,
        peer_actor=first.actor,
        peer_event_seq=first.seq,
        suffix=f"src={act.source_format}",
        evidence={
            "action_signature": sig,
            "informed": informed,
            "comms_disabled": comms_disabled,
        },
    )
