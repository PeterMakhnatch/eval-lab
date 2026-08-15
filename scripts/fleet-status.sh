#!/bin/bash
# fleet-status.sh — one screen of TRUTHFUL fleet state (M001 rewrite).
#
# Derives, never trusts: branch liveness from git, PR state from gh, mission
# registration from agents/missions/ACTIVE.md, handoff headers from worktrees.
# Squash-spent branches (tree already contained in main, merged PR head, or
# zero commits ahead) are reported as SPENT, never as active work.
#
#   scripts/fleet-status.sh
#
# Testability seams (tests/test_fleet_status.py): every external command and
# path is overridable, so tests inject canned git/gh output and a fixture
# repo — no host branches, no network, no gh auth.
#   FLEET_GIT   git executable            (default: git)
#   FLEET_GH    gh executable, "" = none  (default: gh)
#   FLEET_ROOT  repository root           (default: script's parent repo)
#   FLEET_STALE_HOURS  active-mission staleness threshold (default: 48)
set -uo pipefail

GIT="${FLEET_GIT:-git}"
GH="${FLEET_GH-gh}"
root="${FLEET_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
stale_hours="${FLEET_STALE_HOURS:-48}"
cd "$root" || exit 1

board="agents/missions/ACTIVE.md"
bar() { printf '%s\n' "──────────────────────────────────────────────────────"; }

echo "FLEET STATUS  $(date '+%Y-%m-%d %H:%M')  (root: $root)"
bar

# ---- the board: Now / Review / Next / Needs Peter ---------------------------
echo "## board (from $board)"
if [ -f "$board" ]; then
    awk '/^---$/{exit} /^## /{on=1} on{print "  " $0}' "$board"
else
    echo "  !! $board missing — the sole live board is gone; governance broken"
fi
bar

# ---- merged PR heads (for spent detection); tolerate gh absence -------------
merged_heads=""
gh_note=""
if [ -n "$GH" ] && command -v "${GH%% *}" >/dev/null 2>&1; then
    merged_heads="$("$GH" pr list --state merged --limit 100 \
        --json headRefName --jq '.[].headRefName' 2>/dev/null || true)"
    [ -z "$merged_heads" ] && gh_note=" (gh returned no merged heads)"
else
    gh_note=" (gh unavailable — spent detection uses git only)"
fi

# ---- branches: derive state, never assume -----------------------------------
echo "## branches$gh_note"
now_epoch="$(date +%s)"
active_branches=""
for branch in $("$GIT" for-each-ref --format='%(refname:short)' refs/heads/ | grep '^role/' || true); do
    ahead="$("$GIT" rev-list --count origin/main.."$branch" 2>/dev/null || echo 0)"
    state="active"
    reason=""
    if [ "$ahead" = "0" ]; then
        state="spent"; reason="0 ahead of origin/main"
    elif "$GIT" diff --quiet "origin/main...$branch" 2>/dev/null; then
        state="spent"; reason="tree identical to origin/main (squash-merged)"
    elif [ -n "$merged_heads" ] && printf '%s\n' "$merged_heads" | grep -qx "$branch"; then
        state="spent"; reason="head of a merged PR"
    fi
    if [ "$state" = "spent" ]; then
        echo "  $branch  SPENT — $reason"
        continue
    fi
    active_branches="$active_branches $branch"
    last_epoch="$("$GIT" log -1 --format='%ct' "$branch" 2>/dev/null || echo 0)"
    age_h=$(( (now_epoch - last_epoch) / 3600 ))
    flags=""
    if [ -f "$board" ] && ! grep -q "$branch" "$board"; then
        flags="$flags UNREGISTERED(not-on-board)"
    fi
    [ "$age_h" -ge "$stale_hours" ] && flags="$flags STALE(${age_h}h-since-commit)"
    echo "  $branch  active, +$ahead, last commit ${age_h}h ago${flags:+ —$flags}"

    wt="$("$GIT" worktree list --porcelain 2>/dev/null \
        | grep -B2 "branch refs/heads/$branch" | grep '^worktree' | cut -d' ' -f2)"
    if [ -n "$wt" ] && [ -d "$wt" ]; then
        dirty="$("$GIT" -C "$wt" status --short 2>/dev/null | wc -l | tr -d ' ')"
        [ "$dirty" != "0" ] && echo "    uncommitted: $dirty file(s) in $wt"
        handoff="$(ls "$wt"/agents/handoffs/*.md 2>/dev/null \
            | xargs grep -l "^Status:" 2>/dev/null \
            | xargs ls -t 2>/dev/null | head -1)"
        if [ -n "$handoff" ]; then
            grep -E '^(Status|Last|Next|Blockers):' "$handoff" | sed 's/^/    /'
        else
            echo "    handoff: MISSING for this worktree"
        fi
    else
        echo "    worktree: none attached"
    fi
done
[ -z "$active_branches" ] && echo "  (no active role/ branches)"
bar

# ---- board entries whose branch/worktree no longer exists -------------------
if [ -f "$board" ]; then
    echo "## board hygiene"
    hygiene_ok=1
    while IFS= read -r b; do
        if ! "$GIT" for-each-ref --format='%(refname:short)' refs/heads/ | grep -qx "$b"; then
            echo "  !! board lists $b but no such local branch — stale entry or remote-only"
            hygiene_ok=0
        fi
    done < <(grep -oE 'role/[a-z0-9-]+' "$board" | sort -u)
    [ "$hygiene_ok" = "1" ] && echo "  board branches all exist locally"
    bar
fi

# ---- open PRs ---------------------------------------------------------------
echo "## open pull requests"
if [ -n "$GH" ] && command -v "${GH%% *}" >/dev/null 2>&1; then
    "$GH" pr list --state open 2>/dev/null | sed 's/^/  /' || echo "  (gh error)"
else
    echo "  (gh unavailable)"
fi
bar

# ---- queue + digest (informational, tolerant) -------------------------------
echo "## queue"
if [ -d queue ]; then
    for state in proposed pending waiting approved running done failed rejected; do
        [ -d "queue/$state" ] || continue
        n="$(find "queue/$state" -name '*.json' 2>/dev/null | wc -l | tr -d ' ')"
        [ "$n" != "0" ] && echo "  $state: $n"
    done
    [ -f queue/STOP ] && echo "  !! STOP file present — dispatch halted"
else
    echo "  (no queue directory)"
fi
bar

echo "## latest digest"
latest_digest="$(ls -1 digests/*.md 2>/dev/null | sort | tail -1)"
if [ -n "${latest_digest:-}" ]; then
    echo "  $latest_digest"
else
    echo "  (none)"
fi
bar

# ---- recent lab events (rotated segments + live tail, contract-tested) ------
echo "## recent lab events"
event_segments="$(find queue -maxdepth 1 -type f -name 'events.jsonl.*' 2>/dev/null | sort -t. -k3,3nr)"
if [ -f queue/events.jsonl ] || [ -n "$event_segments" ]; then
    {
        while IFS= read -r segment; do
            [ -n "$segment" ] && cat "$segment"
        done <<EOF
$event_segments
EOF
        [ -f queue/events.jsonl ] && cat queue/events.jsonl
    } | tail -8 | sed 's/^/  /'
else
    echo "  (no events.jsonl yet)"
fi
