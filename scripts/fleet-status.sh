#!/bin/bash
# fleet-status.sh — one screen of TRUTHFUL fleet state (M001 rewrite).
#
# Derives branch/worktree state from git, PR state from gh, and pickup-counter
# claims from research/inbox/claims/. This report never authorizes cleanup.
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

board="research/inbox/board.md"
bar() { printf '%s\n' "──────────────────────────────────────────────────────"; }

echo "FLEET STATUS  $(date '+%Y-%m-%d %H:%M')  (root: $root)"
bar

# ---- the board: status & claims ---------------------------------------------
echo "## board (from $board)"
if [ -f "$board" ]; then
    awk '/^(# What lives where|---)$/{exit} /^## /{on=1} on{print "  " $0}' "$board"
else
    echo "  !! $board missing — the sole live board is gone; governance broken"
fi
bar

echo "## pickup-counter claims"
claims_found=0
for claim in research/inbox/claims/*.md; do
    [ -f "$claim" ] || continue
    [ "$claim" = "research/inbox/claims/README.md" ] && continue
    claims_found=1
    echo "  $claim"
    sed -n -E '/^(item|role|why-me|branch|handoff):/s/^/    /p' "$claim"
done
[ "$claims_found" = 1 ] || echo "  (no claim files; historical board summaries are not live presence)"
bar

# ---- merged PR heads (for spent detection); tolerate gh absence -------------
merged_refs=""
merged_heads=""
merged_oids=""
gh_note=""
if [ -n "$GH" ] && command -v "${GH%% *}" >/dev/null 2>&1; then
    merged_refs="$("$GH" pr list --state merged --limit 500 \
        --json headRefName,headRefOid \
        --jq '.[] | [.headRefName, .headRefOid] | @tsv' 2>/dev/null || true)"
    merged_heads="$(printf '%s\n' "$merged_refs" | cut -f1)"
    merged_oids="$(printf '%s\n' "$merged_refs" | cut -f2)"
    [ -z "$merged_heads" ] && gh_note=" (gh returned no merged heads)"
else
    gh_note=" (gh unavailable — spent detection uses git only)"
fi

# ---- branches: derive state, never assume -----------------------------------
echo "## branches$gh_note"
now_epoch="$(date +%s)"
active_branches=""
worktree_inventory="$("$GIT" worktree list --porcelain 2>/dev/null || true)"

# Check git capability for ahead-behind atom (git >= 2.41)
has_ahead_behind=0
git_ver="$("$GIT" --version 2>/dev/null || true)"
if [[ "$git_ver" =~ ([0-9]+)\.([0-9]+) ]]; then
    major="${BASH_REMATCH[1]}"
    minor="${BASH_REMATCH[2]}"
    if [ "$major" -gt 2 ] || { [ "$major" -eq 2 ] && [ "$minor" -ge 41 ]; }; then
        has_ahead_behind=1
    fi
fi

if [ "$has_ahead_behind" -eq 0 ]; then
    echo "  (git < 2.41 — %(ahead-behind) unsupported; falling back to per-branch git calls)"
    for branch in $("$GIT" for-each-ref --format='%(refname:short)' refs/heads/); do
        case "$branch" in main|integrate/*) continue ;; esac
        if ! ahead="$("$GIT" rev-list --count origin/main.."$branch" 2>/dev/null)"; then
            echo "  $branch  UNKNOWN — cannot compare with origin/main; preserve"
            continue
        fi
        branch_oid="$("$GIT" rev-parse "$branch" 2>/dev/null || true)"
        wt="$(printf '%s\n' "$worktree_inventory" | awk -v ref="refs/heads/$branch" '
            /^worktree / { path=substr($0, 10) }
            /^branch / && substr($0, 8)==ref { print path }
        ')"
        dirty="0"
        if [ -n "$wt" ] && [ -d "$wt" ]; then
            if ! changes="$("$GIT" -C "$wt" status --short 2>/dev/null)"; then
                echo "  $branch  UNKNOWN — worktree status unavailable; preserve"
                continue
            fi
            [ -z "$changes" ] || dirty="$(printf '%s\n' "$changes" | wc -l | tr -d ' ')"
        fi
        state="active"
        reason=""
        if [ "$dirty" != "0" ]; then
            state="active"; reason="uncommitted worktree changes"
        elif [ "$ahead" = "0" ]; then
            state="spent"; reason="0 ahead of origin/main"
        elif "$GIT" diff --quiet origin/main "$branch" 2>/dev/null; then
            state="spent"; reason="tree identical to origin/main"
        elif [ -n "$branch_oid" ] && [ -n "$merged_oids" ] \
            && printf '%s\n' "$merged_oids" | grep -qx "$branch_oid"; then
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
        [ "$age_h" -ge "$stale_hours" ] && flags="$flags STALE(${age_h}h-since-commit)"
        echo "  $branch  active, +$ahead, last commit ${age_h}h ago${flags:+ —$flags}"

        if [ -n "$wt" ] && [ -d "$wt" ]; then
            [ "$dirty" != "0" ] && echo "    uncommitted: $dirty file(s) in $wt"
            echo "    worktree: $wt (intent comes from pickup-counter claims above)"
        else
            echo "    worktree: none attached"
        fi
    done
    [ -z "$active_branches" ] && echo "  (no active topic branches)"
    bar
else
    wt_dirty_tmp="$(mktemp "${TMPDIR:-/tmp}/fleet_wt_dirty.XXXXXX")"
    wt_pairs_tmp="$(mktemp "${TMPDIR:-/tmp}/fleet_wt_pairs.XXXXXX")"
    need_diff_tmp="$(mktemp "${TMPDIR:-/tmp}/fleet_need_diff.XXXXXX")"
    tree_spent_tmp="$(mktemp "${TMPDIR:-/tmp}/fleet_tree_spent.XXXXXX")"
    branch_refs_tmp="$(mktemp "${TMPDIR:-/tmp}/fleet_branch_refs.XXXXXX")"
    trap 'rm -f "$wt_dirty_tmp" "$wt_pairs_tmp" "$need_diff_tmp" "$tree_spent_tmp" "$branch_refs_tmp"' EXIT

    # Extract topic branch worktrees
    printf '%s\n' "$worktree_inventory" | awk '
        /^worktree / { wt=substr($0, 10) }
        /^branch refs\/heads\// {
            br=substr($0, 19)
            if (wt != "" && br != "" && br != "main" && br !~ /^integrate\//) {
                print wt "\t" br
            }
        }
    ' > "$wt_pairs_tmp"

    if [ -s "$wt_pairs_tmp" ]; then
        check_worktree() {
            local wt="$1"
            local branch="$2"
            if [ ! -d "$wt" ]; then
                printf '%s\t\t0\n' "$branch"
                return
            fi
            local changes
            if ! changes="$("$GIT" -C "$wt" status --porcelain 2>/dev/null)"; then
                printf '%s\t%s\tERROR\n' "$branch" "$wt"
                return
            fi
            local count=0
            [ -n "$changes" ] && count="$(printf '%s\n' "$changes" | wc -l | tr -d ' ')"
            printf '%s\t%s\t%s\n' "$branch" "$wt" "$count"
        }
        export -f check_worktree
        export GIT

        tr '\t\n' '\0\0' < "$wt_pairs_tmp" \
            | xargs -0 -n 2 -P 4 bash -c 'check_worktree "$1" "$2"' dummy \
            | sort -k1,1 > "$wt_dirty_tmp"
    fi

    # Branch metadata in a single git call
    "$GIT" for-each-ref --format='%(refname:short)%09%(objectname)%09%(committerdate:unix)%09%(ahead-behind:origin/main)' refs/heads/ > "$branch_refs_tmp" 2>/dev/null || true

    # Identify branches that need diff check (ahead > 0 and dirty == 0 and not in merged_oids)
    export MERGED_OIDS="$merged_oids"
    awk -F'\t' '
        BEGIN {
            split(ENVIRON["MERGED_OIDS"], m_lines, "\n")
            for (i in m_lines) if (m_lines[i] != "") m_set[m_lines[i]] = 1
        }
        NR==FNR {
            dirty_map[$1] = $3
            next
        }
        {
            branch = $1
            if (branch == "main" || branch ~ /^integrate\//) next
            oid = $2
            ahead_behind = $4
            split(ahead_behind, ab, " ")
            ahead = ab[1]
            dirty = (branch in dirty_map) ? dirty_map[branch] : "0"

            if (ahead ~ /^[0-9]+$/ && ahead > 0 && dirty == "0") {
                if (!(oid in m_set)) {
                    print branch
                }
            }
        }
    ' "$wt_dirty_tmp" "$branch_refs_tmp" > "$need_diff_tmp"

    if [ -s "$need_diff_tmp" ]; then
        check_tree() {
            local b="$1"
            if "$GIT" diff --quiet --no-renames origin/main "$b" 2>/dev/null; then
                printf '%s\n' "$b"
            fi
        }
        export -f check_tree
        xargs -P 8 -n 1 bash -c 'check_tree "$1"' dummy < "$need_diff_tmp" | sort > "$tree_spent_tmp"
    fi

    # Render branches deterministically using awk (O(1) in-memory formatting)
    awk -F'\t' \
        -v now_epoch="$now_epoch" \
        -v stale_hours="$stale_hours" '
    BEGIN {
        split(ENVIRON["MERGED_OIDS"], m_lines, "\n")
        for (i in m_lines) if (m_lines[i] != "") m_set[m_lines[i]] = 1
        active_count = 0
    }
    FILENAME == ARGV[1] {
        wt_map[$1] = $2
        dirty_map[$1] = $3
        next
    }
    FILENAME == ARGV[2] {
        tree_spent[$1] = 1
        next
    }
    {
        branch = $1
        if (branch == "" || branch == "main" || branch ~ /^integrate\//) next
        branch_oid = $2
        last_epoch = ($3 != "") ? $3 + 0 : 0
        ahead_behind = $4
        split(ahead_behind, ab, " ")
        ahead = ab[1]

        if (ahead == "" || ahead !~ /^[0-9]+$/) {
            print "  " branch "  UNKNOWN — cannot compare with origin/main; preserve"
            next
        }

        wt = ""
        dirty = "0"
        if (branch in wt_map) {
            wt = wt_map[branch]
            wt_status = dirty_map[branch]
            if (wt_status == "ERROR") {
                print "  " branch "  UNKNOWN — worktree status unavailable; preserve"
                next
            }
            dirty = (wt_status != "") ? wt_status : "0"
        }

        state = "active"
        reason = ""
        if (dirty != "0") {
            state = "active"
            reason = "uncommitted worktree changes"
        } else if (ahead == 0) {
            state = "spent"
            reason = "0 ahead of origin/main"
        } else if (branch in tree_spent) {
            state = "spent"
            reason = "tree identical to origin/main"
        } else if (branch_oid != "" && (branch_oid in m_set)) {
            state = "spent"
            reason = "head of a merged PR"
        }

        if (state == "spent") {
            print "  " branch "  SPENT — " reason
            next
        }

        active_count++
        age_h = int((now_epoch - last_epoch) / 3600)
        flags = ""
        if (age_h >= stale_hours) {
            flags = " — STALE(" age_h "h-since-commit)"
        }
        print "  " branch "  active, +" ahead ", last commit " age_h "h ago" flags

        if (wt != "") {
            if (dirty != "0") {
                print "    uncommitted: " dirty " file(s) in " wt
            }
            print "    worktree: " wt " (intent comes from pickup-counter claims above)"
        } else {
            print "    worktree: none attached"
        }
    }
    END {
        if (active_count == 0) {
            print "  (no active topic branches)"
        }
    }
    ' "$wt_dirty_tmp" "$tree_spent_tmp" "$branch_refs_tmp"
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
