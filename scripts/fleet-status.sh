#!/bin/bash
# fleet-status.sh — one-screen answer to "what have all the agents done?"
# Read-only: aggregates git branches, worktrees, HANDOFF files, PRs, queue
# state, digests, and events. Safe to run at any time from anywhere.
#
#   scripts/fleet-status.sh            # full report
#   scripts/fleet-status.sh --since 6h # only commits from the last N hours
#
# BUILDER: brief 12 (docs/fleet-tracking.md) absorbs this into
# `harbor-lab fleet` with the same sections; keep the section names stable.
set -uo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

since="24 hours ago"
if [ "${1:-}" = "--since" ] && [ -n "${2:-}" ]; then
    since="${2/h/ hours ago}"
fi

bar() { printf '%s\n' "──────────────────────────────────────────────────────"; }

echo "FLEET STATUS  $(date '+%Y-%m-%d %H:%M')  (commits since: $since)"
bar

echo "## main"
git log main --oneline -3 | sed 's/^/  /'
dirty="$(git status --short | head -8)"
if [ -n "$dirty" ]; then
    echo "  [BUILDER working tree is dirty — work in progress:]"
    echo "$dirty" | sed 's/^/    /'
fi
bar

echo "## roles"
for branch in $(git for-each-ref --format='%(refname:short)' refs/heads/ | grep '^role/'); do
    role="${branch#role/}"
    wt="$(git worktree list --porcelain | grep -B2 "branch refs/heads/$branch" | grep '^worktree' | cut -d' ' -f2)"
    ahead="$(git rev-list --count main.."$branch" 2>/dev/null || echo '?')"
    last="$(git log "$branch" -1 --format='%ar — %s' 2>/dev/null || echo 'no commits')"
    echo "  $role  [+$ahead ahead of main]"
    echo "    last commit: $last"
    recent="$(git log "$branch" --oneline --since="$since" --not main 2>/dev/null | head -5)"
    [ -n "$recent" ] && echo "$recent" | sed 's/^/    /'
    if [ -n "$wt" ] && [ -d "$wt" ]; then
        wt_dirty="$(git -C "$wt" status --short 2>/dev/null | wc -l | tr -d ' ')"
        [ "$wt_dirty" != "0" ] && echo "    uncommitted changes in worktree: $wt_dirty file(s)"
        handoff="$wt/agents/handoffs/$role.md"
        [ -f "$handoff" ] || handoff="$(find "$wt" -maxdepth 3 -name HANDOFF.md -not -path '*/.venv/*' 2>/dev/null | head -1)"
        if [ -n "$handoff" ]; then
            echo "    HANDOFF:"
            grep -E '^(Status|Last|Next|Blockers):' "$handoff" 2>/dev/null | sed 's/^/      /' \
                || tail -5 "$handoff" | sed 's/^/      /'
        else
            echo "    HANDOFF: missing"
        fi
    else
        echo "    worktree: not set up"
    fi
done
bar

echo "## pull requests"
gh pr list --state all -L 10 2>/dev/null | sed 's/^/  /' || echo "  (gh unavailable)"
bar

echo "## queue"
if [ -d queue ]; then
    for state in proposed pending waiting approved running done failed rejected; do
        [ -d "queue/$state" ] || continue
        n="$(find "queue/$state" -name '*.json' 2>/dev/null | wc -l | tr -d ' ')"
        [ "$n" != "0" ] && echo "  $state: $n"
    done
    [ -f queue/STOP ] && echo "  !! STOP file present — dispatch halted"
else
    echo "  (no queue yet — brief 05 pending or not merged)"
fi
bar

echo "## latest digest"
latest_digest="$(ls -1 digests/*.md 2>/dev/null | sort | tail -1)"
if [ -n "${latest_digest:-}" ]; then
    echo "  $latest_digest"
    head -15 "$latest_digest" | sed 's/^/  /'
else
    echo "  (none yet — brief 06 pending)"
fi
bar

echo "## recent lab events"
if [ -f events.jsonl ]; then
    tail -8 events.jsonl | sed 's/^/  /'
else
    echo "  (no events.jsonl yet)"
fi
