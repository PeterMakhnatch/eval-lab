from __future__ import annotations

import re
from bisect import bisect_right
from collections import Counter, defaultdict
from typing import Any

from ._forensics_join import extract_timed_atif_file_reads
from ._forensics_rules import (
    normalize_repo_path,
    read_targets,
    trunk_branches_for_log,
    write_targets,
)
from ._forensics_sweep import log_group
from ._multiagent_io import NormalizedAtifStep
from .world_events import NormalizedWorldEvent, WorldEventLog

BRANCH_RE = re.compile(r"\bbranch(?:es)?\b[^`'\"\w]{0,4}[`'\"]?([A-Za-z0-9][\w./-]*[\w-])", re.I)
BRANCH_STOPWORDS = frozenset(
    {"name", "names", "for", "from", "to", "and", "is", "with", "main", "master"}
)


def _find_intervening_seq_write(
    writes: list[tuple[int, str]],
    prev_r: int,
    actor: str,
    ev_seq: int,
) -> tuple[int, str] | None:
    for w_seq, w_actor in reversed(writes):
        if w_seq <= prev_r:
            break
        if w_actor != actor and w_seq < ev_seq:
            return (w_seq, w_actor)
    return None


def _collect_world_stale_races(
    events: list[NormalizedWorldEvent],
    roster_set: set[str],
    seen: set[tuple[str, int, str]],
    per_member: dict[str, int],
    details: list[dict[str, Any]],
) -> None:
    last_read_seq: dict[tuple[str, str, str], int] = {}
    writes_seq: dict[tuple[str, str], list[tuple[int, str]]] = defaultdict(list)
    ordered_events = sorted(
        (
            ev
            for ev in events
            if ev.phase != "post_play" and not ev.late and ev.accepted and ev.actor in roster_set
        ),
        key=lambda ev: (log_group(ev.source_format), ev.seq),
    )
    for ev in ordered_events:
        assert ev.actor is not None
        grp = log_group(ev.source_format)
        if ev.kind == "world_read":
            for target in read_targets(ev):
                last_read_seq[(grp, ev.actor, target)] = ev.seq
        elif ev.kind in ("world_action", "artifact_write"):
            for target in write_targets(ev):
                prev_r = last_read_seq.get((grp, ev.actor, target))
                if prev_r is not None:
                    hit_w = _find_intervening_seq_write(
                        writes_seq[(grp, target)], prev_r, ev.actor, ev.seq
                    )
                    if hit_w is not None and (ev.source_format, ev.seq, target) not in seen:
                        seen.add((ev.source_format, ev.seq, target))
                        per_member[ev.actor] += 1
                        details.append(
                            {
                                "kind": "stale_read_write_race",
                                "actor": ev.actor,
                                "target": target,
                                "read_seq": prev_r,
                                "intervening_writer": hit_w[1],
                                "intervening_write_seq": hit_w[0],
                                "write_seq": ev.seq,
                            }
                        )
                writes_seq[(grp, target)].append((ev.seq, ev.actor))
                last_read_seq[(grp, ev.actor, target)] = ev.seq


def _build_git_race_timeline(
    reads: list[tuple[float, str, str]],
    git_events: list[NormalizedWorldEvent],
    roster_set: set[str],
) -> tuple[
    list[tuple[float, int, str, str, str, str | None, str | None, str, int]],
    dict[tuple[str, str], list[float]],
]:
    timeline: list[tuple[float, int, str, str, str, str | None, str | None, str, int]] = [
        (ts, 0, "read", actor, normalize_repo_path(path), None, None, "", 0)
        for ts, actor, path in reads
        if actor in roster_set
    ]
    for gev in git_events:
        if (
            gev.phase == "post_play"
            or gev.late
            or not gev.accepted
            or gev.actor not in roster_set
            or gev.ts_epoch is None
        ):
            continue
        for path in gev.files:
            timeline.append(
                (
                    gev.ts_epoch,
                    1,
                    "write",
                    gev.actor,
                    normalize_repo_path(path),
                    gev.commit_sha,
                    gev.branch,
                    gev.source_format,
                    gev.seq,
                )
            )
    reads_by_at: dict[tuple[str, str], list[float]] = defaultdict(list)
    for ts_r, r_actor, r_path in reads:
        if r_actor in roster_set:
            reads_by_at[(r_actor, normalize_repo_path(r_path))].append(ts_r)
    for r_list in reads_by_at.values():
        r_list.sort()
    timeline.sort(key=lambda item: (item[0], item[1], item[3], item[4]))
    return timeline, reads_by_at


def _find_intervening_git_race(
    writes: list[tuple[float, str, str | None]],
    actor: str,
    prev_ts: float,
    ts: float,
    branch: str | None,
    src_fmt: str,
    r_list: list[float] | tuple[float, ...],
) -> tuple[float, str] | None:
    hit_ts: tuple[float, str] | None = None
    for w_ts, w_actor, w_branch in reversed(writes):
        if w_ts <= prev_ts:
            break
        if (
            w_actor != actor
            and w_ts < ts
            and (w_branch is None or branch is None or w_branch == branch)
        ):
            hit_ts = (w_ts, w_actor)
            break
    if hit_ts is not None:
        r_idx = bisect_right(r_list, hit_ts[0])
        hi_ts = ts + (1.0 if src_fmt == "bespoke_git_bundle" else 0.0)
        if r_idx < len(r_list) and r_list[r_idx] <= hi_ts:
            return None
    return hit_ts


def _collect_git_stale_races(
    git_events: list[NormalizedWorldEvent],
    steps_by_member: dict[str, list[NormalizedAtifStep]],
    roster_set: set[str],
    timed_reads: list[tuple[float, str, str]] | None,
    seen: set[tuple[str, int, str]],
    per_member: dict[str, int],
    details: list[dict[str, Any]],
) -> None:
    reads = (
        timed_reads if timed_reads is not None else extract_timed_atif_file_reads(steps_by_member)
    )
    timeline, reads_by_at = _build_git_race_timeline(reads, git_events, roster_set)
    last_read_ts: dict[tuple[str, str], float] = {}
    last_write_ts: dict[tuple[str, str, str | None], float] = {}
    writes_ts: dict[str, list[tuple[float, str, str | None]]] = defaultdict(list)
    for ts, _, op, actor, target, sha, branch, src_fmt, g_seq in timeline:
        if op == "read":
            last_read_ts[(actor, target)] = ts
            continue
        r_prev = last_read_ts.get((actor, target))
        w_prev = last_write_ts.get((actor, target, branch))
        prev_ts = (
            max(r_prev, w_prev)
            if r_prev is not None and w_prev is not None
            else (r_prev if r_prev is not None else w_prev)
        )
        if prev_ts is not None:
            hit_ts = _find_intervening_git_race(
                writes_ts[target],
                actor,
                prev_ts,
                ts,
                branch,
                src_fmt,
                reads_by_at.get((actor, target), ()),
            )
            if hit_ts is not None and (src_fmt, g_seq, target) not in seen:
                seen.add((src_fmt, g_seq, target))
                per_member[actor] += 1
                details.append(
                    {
                        "kind": "stale_read_write_race",
                        "actor": actor,
                        "target": target,
                        "intervening_writer": hit_ts[1],
                        "commit_sha": sha,
                    }
                )
        writes_ts[target].append((ts, actor, branch))
        last_write_ts[(actor, target, branch)] = ts


def compute_stale_races(
    events: list[NormalizedWorldEvent],
    git_events: list[NormalizedWorldEvent],
    steps_by_member: dict[str, list[NormalizedAtifStep]],
    roster_set: set[str],
    *,
    timed_reads: list[tuple[float, str, str]] | None = None,
) -> tuple[int, dict[str, int], list[dict[str, Any]]]:
    per_member: dict[str, int] = defaultdict(int)
    details: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    _collect_world_stale_races(events, roster_set, seen, per_member, details)
    if git_events:
        _collect_git_stale_races(
            git_events, steps_by_member, roster_set, timed_reads, seen, per_member, details
        )
    return sum(per_member.values()), dict(per_member), details


def _collect_git_shared_owners(
    world_log: WorldEventLog,
    roster_set: set[str],
    in_play_git: list[NormalizedWorldEvent] | None,
    world_file_owners: dict[str, set[str]],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    shared_files: dict[str, list[str]] = {}
    git_branch_owners: dict[str, set[str]] = defaultdict(set)
    if not world_log.git_events:
        for raw_k, owners_list in world_log.git_shared_files.items():
            nk = normalize_repo_path(raw_k)
            shared_files[nk] = sorted(set(shared_files.get(nk, [])) | set(owners_list))
        return shared_files, dict(sorted(world_log.git_shared_branches.items()))

    git_src = (
        in_play_git
        if in_play_git is not None
        else [ev for ev in world_log.git_events if ev.phase != "post_play" and not ev.late]
    )
    in_play_sha_actor: dict[str, str] = {}
    for gev in git_src:
        if not gev.accepted or gev.actor not in roster_set or gev.detail.get("is_seed"):
            continue
        for fpath in gev.files:
            world_file_owners[normalize_repo_path(fpath)].add(gev.actor)
        if gev.commit_sha:
            in_play_sha_actor[gev.commit_sha] = gev.actor
        elif gev.branch and gev.branch not in ("main", "master"):
            git_branch_owners[gev.branch].add(gev.actor)
    if world_log.git_commits:
        allowed_owners = {b: set(owners) for b, owners in world_log.git_shared_branches.items()}
        trunk_branches = trunk_branches_for_log(world_log)
        for commit in world_log.git_commits:
            actor = in_play_sha_actor.get(commit.sha)
            if actor is None or commit.is_root_commit or (trunk_branches & set(commit.branches)):
                continue
            for b in commit.branches:
                if b in allowed_owners and actor in allowed_owners[b]:
                    git_branch_owners[b].add(actor)
    else:
        for gev in git_src:
            if (
                gev.accepted
                and gev.actor in roster_set
                and not gev.detail.get("is_seed")
                and gev.branch
                and gev.branch not in ("main", "master")
            ):
                git_branch_owners[gev.branch].add(gev.actor)
    shared_branches = {
        b: sorted(owners) for b, owners in sorted(git_branch_owners.items()) if len(owners) > 1
    }
    return shared_files, shared_branches


SharedConflictsResult = tuple[
    int,
    dict[str, list[str]],
    dict[str, list[str]],
    dict[str, list[str]],
    Counter[str],
    Counter[str],
    set[str],
    list[dict[str, Any]],
]


def _collect_proposed_collisions(
    peer_posts: list[NormalizedWorldEvent],
) -> dict[str, list[str]]:
    proposed_branches: dict[str, set[str]] = defaultdict(set)
    for post in peer_posts:
        if post.actor and post.text:
            for branch in BRANCH_RE.findall(post.text):
                if branch.lower() not in BRANCH_STOPWORDS:
                    proposed_branches[branch].add(post.actor)
    return {
        b: sorted(owners) for b, owners in sorted(proposed_branches.items()) if len(owners) > 1
    }


def compute_shared_conflicts(
    in_play_events: list[NormalizedWorldEvent],
    peer_posts: list[NormalizedWorldEvent],
    world_log: WorldEventLog,
    roster_set: set[str],
    *,
    in_play_git: list[NormalizedWorldEvent] | None = None,
) -> SharedConflictsResult:
    world_file_owners: dict[str, set[str]] = defaultdict(set)
    overwrites_by_member: Counter[str] = Counter()
    details: list[dict[str, Any]] = []
    for ev in in_play_events:
        if ev.kind == "artifact_write" and ev.accepted and ev.actor in roster_set:
            for fpath in ev.files:
                world_file_owners[normalize_repo_path(fpath)].add(ev.actor)
            if ev.overwrites_actor and ev.overwrites_actor != ev.actor:
                overwrites_by_member[ev.actor] += 1
                details.append(
                    {
                        "kind": "artifact_overwrite",
                        "actor": ev.actor,
                        "overwrites_actor": ev.overwrites_actor,
                        "files": ev.files,
                        "seq": ev.seq,
                    }
                )
    shared_files, shared_branches = _collect_git_shared_owners(
        world_log, roster_set, in_play_git, world_file_owners
    )
    for fpath, owners in sorted(world_file_owners.items()):
        if len(owners) > 1:
            shared_files[fpath] = sorted(set(shared_files.get(fpath, [])) | owners)
    shared_files = dict(sorted(shared_files.items()))
    proposed_collisions = _collect_proposed_collisions(peer_posts)
    extra_proposed = set(proposed_collisions) - set(shared_branches)
    extra_overwrites_by_member: Counter[str] = Counter(
        ev.actor
        for ev in in_play_events
        if ev.kind == "artifact_write"
        and ev.accepted
        and ev.actor in roster_set
        and ev.overwrites_actor
        and ev.overwrites_actor != ev.actor
        and not any(normalize_repo_path(f) in shared_files for f in ev.files)
    )
    total = (
        len(shared_branches)
        + len(shared_files)
        + len(extra_proposed)
        + sum(extra_overwrites_by_member.values())
    )
    return (
        total,
        shared_branches,
        shared_files,
        proposed_collisions,
        overwrites_by_member,
        extra_overwrites_by_member,
        extra_proposed,
        details,
    )
