#!/bin/bash
# sync-primary-checkout.sh — fast-forward a primary checkout to its remote,
# never discarding local changes.
#
# What it does, in order:
#   1. Takes an atomic mkdir lock so concurrent timer runs cannot overlap.
#   2. Fetches the remote (plain `git fetch`: only remote-tracking refs move,
#      which is worktree-safe).
#   3. Fast-forwards the checkout with `git merge --ff-only` — but ONLY when
#      every guard holds: HEAD is on the expected branch, the tree is clean,
#      no merge/rebase/cherry-pick is in progress, and local HEAD is an
#      ancestor of the remote ref.
#   4. Otherwise skips and records the reason (dirty with N files, off-branch,
#      diverged with ahead/behind counts, in-progress op, fetch failure) in a
#      status JSON file, plus a rate-limited macOS notification.
#
# What it never does: reset, stash, clean, checkout, or force-push. A dirty,
# off-branch, or diverged checkout is someone's work; this script only reports.
#
# Untracked files DO count as dirty (see --untracked-files=all below): an
# untracked file can collide with an incoming path — in which case even
# --ff-only would refuse mid-merge — and unknown files mean unknown provenance,
# so skip-and-report is the conservative default.
#
# Configuration (environment, all optional):
#   EVALLAB_SYNC_REPO                   checkout to sync (default ~/Developer/eval-lab)
#   EVALLAB_SYNC_STATE_DIR              dir for status/lock files
#                                       (default ~/Library/Application Support/evallab)
#   EVALLAB_SYNC_BRANCH                 expected branch (default main)
#   EVALLAB_SYNC_REMOTE                 remote name (default origin)
#   EVALLAB_SYNC_NOTIFY_EVERY_SECONDS   slowest repeat notification for an
#                                       unchanged state (default 21600 = 6h;
#                                       any state *change* always notifies)
#   EVALLAB_SYNC_NO_NOTIFY=1            disable osascript notifications (tests)
#
# Exit status: 0 on sync, up-to-date, or any expected skip. Non-zero only on
# script-internal errors (bad usage, unwritable state dir, git not found).
set -euo pipefail

REPO="${EVALLAB_SYNC_REPO:-$HOME/Developer/eval-lab}"
STATE_DIR="${EVALLAB_SYNC_STATE_DIR:-$HOME/Library/Application Support/evallab}"
BRANCH="${EVALLAB_SYNC_BRANCH:-main}"
REMOTE="${EVALLAB_SYNC_REMOTE:-origin}"
NOTIFY_EVERY_SECONDS="${EVALLAB_SYNC_NOTIFY_EVERY_SECONDS:-21600}"
NO_NOTIFY="${EVALLAB_SYNC_NO_NOTIFY:-0}"

STATUS_FILE="$STATE_DIR/sync-status.json"
NOTIFY_FILE="$STATE_DIR/sync-notify.json"
LOCK_DIR="$STATE_DIR/sync-primary.lock"
PID_FILE="$LOCK_DIR/pid"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }

# --- lock (atomic mkdir; stale PIDs reclaimed) -------------------------------
if ! mkdir "$STATE_DIR" 2>/dev/null; then
  if [ ! -d "$STATE_DIR" ]; then
    log "ERROR: cannot create state dir: $STATE_DIR"
    exit 2
  fi
fi

if mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "$$" >"$PID_FILE"
  trap 'rm -rf "$LOCK_DIR"' EXIT
else
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    log "another sync run is in progress; exiting"
    exit 0
  fi
  log "removing stale lock"
  rm -rf "$LOCK_DIR"
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    log "another sync run won the race; exiting"
    exit 0
  fi
  echo "$$" >"$PID_FILE"
  trap 'rm -rf "$LOCK_DIR"' EXIT
fi

command -v git >/dev/null || { log "ERROR: git not found"; exit 2; }

REMOTE_REF="$REMOTE/$BRANCH"
NOW="$(date -u +%FT%TZ)"
FETCH_OK=true
FETCH_ERROR=""
OUTCOME=""
REASON=""
HUMAN=""
DIRTY_N=0
AHEAD=0
BEHIND=0
HEAD_SHA=""
ORIGIN_SHA=""
CURRENT_BRANCH=""

# --- fetch -------------------------------------------------------------------
if ! FETCH_ERR="$(git -C "$REPO" fetch "$REMOTE" 2>&1)"; then
  FETCH_OK=false
  FETCH_ERROR="$(printf '%s' "$FETCH_ERR" | tail -3 | tr '\n' ' ')"
  OUTCOME="skipped"
  REASON="fetch-failed"
  HUMAN="Skipped: fetch from $REMOTE failed ($FETCH_ERROR)."
else
  CURRENT_BRANCH="$(git -C "$REPO" symbolic-ref --short HEAD 2>/dev/null || echo "detached")"
  HEAD_SHA="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo "")"
  ORIGIN_SHA="$(git -C "$REPO" rev-parse --verify "$REMOTE_REF" 2>/dev/null || echo "")"
  # Ahead/behind counts are reported on every path (including dirty/off-branch
  # skips) so the status file always says how far behind the checkout is.
  if [ -n "$HEAD_SHA" ] && [ -n "$ORIGIN_SHA" ]; then
    COUNTS="$(git -C "$REPO" rev-list --left-right --count "HEAD...$REMOTE_REF" 2>/dev/null || echo "0	0")"
    AHEAD="$(printf '%s' "$COUNTS" | cut -f1)"
    BEHIND="$(printf '%s' "$COUNTS" | cut -f2)"
  fi

  if [ "$CURRENT_BRANCH" != "$BRANCH" ]; then
    OUTCOME="skipped"
    REASON="off-branch"
    HUMAN="Skipped: on '$CURRENT_BRANCH', not $BRANCH."
  else
    # Untracked-but-not-ignored files count as dirty (see header).
    PORCELAIN="$(git -C "$REPO" status --porcelain=v1 --untracked-files=all 2>/dev/null || echo "?? status-failed")"
    if [ -z "$PORCELAIN" ]; then
      DIRTY_N=0
    else
      DIRTY_N="$(printf '%s\n' "$PORCELAIN" | wc -l | tr -d ' ')"
    fi
    if [ "$DIRTY_N" -gt 0 ]; then
      OUTCOME="skipped"
      REASON="dirty"
      HUMAN="Skipped: working tree dirty ($DIRTY_N files)."
    else
      GIT_DIR="$(git -C "$REPO" rev-parse --absolute-git-dir)"
      IN_PROGRESS=""
      [ -e "$GIT_DIR/MERGE_HEAD" ] && IN_PROGRESS="merge"
      [ -e "$GIT_DIR/CHERRY_PICK_HEAD" ] && IN_PROGRESS="${IN_PROGRESS:+$IN_PROGRESS,}cherry-pick"
      [ -e "$GIT_DIR/REVERT_HEAD" ] && IN_PROGRESS="${IN_PROGRESS:+$IN_PROGRESS,}revert"
      [ -d "$GIT_DIR/rebase-merge" ] && IN_PROGRESS="${IN_PROGRESS:+$IN_PROGRESS,}rebase"
      [ -d "$GIT_DIR/rebase-apply" ] && IN_PROGRESS="${IN_PROGRESS:+$IN_PROGRESS,}rebase-apply"
      if [ -n "$IN_PROGRESS" ]; then
        OUTCOME="skipped"
        REASON="in-progress"
        HUMAN="Skipped: $IN_PROGRESS in progress."
      elif [ -z "$ORIGIN_SHA" ]; then
        FETCH_OK=false
        FETCH_ERROR="remote ref $REMOTE_REF not found"
        OUTCOME="skipped"
        REASON="fetch-failed"
        HUMAN="Skipped: remote ref $REMOTE_REF not found."
      else
        if ! git -C "$REPO" merge-base --is-ancestor HEAD "$REMOTE_REF" 2>/dev/null; then
          OUTCOME="skipped"
          REASON="diverged"
          HUMAN="Skipped: diverged (ahead $AHEAD, behind $BEHIND)."
        elif [ "$BEHIND" -eq 0 ]; then
          OUTCOME="up-to-date"
          REASON="up-to-date"
          HUMAN="Up to date with $REMOTE_REF."
        else
          if git -C "$REPO" merge --ff-only "$REMOTE_REF" >/dev/null 2>&1; then
            HEAD_SHA="$(git -C "$REPO" rev-parse HEAD)"
            BEHIND=0
            OUTCOME="synced"
            REASON="synced"
            HUMAN="Fast-forwarded $BRANCH to $REMOTE_REF."
          else
            # Unreachable given the guards above, but never fall through
            # silently: report instead of retrying with anything destructive.
            OUTCOME="skipped"
            REASON="diverged"
            HUMAN="Skipped: fast-forward refused (ahead $AHEAD, behind $BEHIND)."
          fi
        fi
      fi
    fi
  fi
fi

STATE="$OUTCOME:$REASON"

# --- status file --------------------------------------------------------------
REPO="$REPO" NOW="$NOW" OUTCOME="$OUTCOME" REASON="$REASON" HUMAN="$HUMAN" \
  CURRENT_BRANCH="$CURRENT_BRANCH" DIRTY_N="$DIRTY_N" AHEAD="$AHEAD" BEHIND="$BEHIND" \
  HEAD_SHA="$HEAD_SHA" ORIGIN_SHA="$ORIGIN_SHA" FETCH_OK="$FETCH_OK" \
  FETCH_ERROR="$FETCH_ERROR" REMOTE_REF="$REMOTE_REF" STATUS_FILE="$STATUS_FILE" \
  python3 - <<'PY'
import json, os
fetch_ok = os.environ["FETCH_OK"] == "true"
payload = {
    "timestamp": os.environ["NOW"],
    "repo": os.environ["REPO"],
    "remote_ref": os.environ["REMOTE_REF"],
    "branch": os.environ.get("CURRENT_BRANCH") or None,
    "outcome": os.environ["OUTCOME"],
    "reason": os.environ["REASON"],
    "message": os.environ["HUMAN"],
    "dirty_files": int(os.environ["DIRTY_N"]),
    "ahead": int(os.environ["AHEAD"]),
    "behind": int(os.environ["BEHIND"]),
    "head_sha": os.environ.get("HEAD_SHA") or None,
    "origin_sha": os.environ.get("ORIGIN_SHA") or None,
    "fetch_ok": fetch_ok,
    "fetch_error": os.environ.get("FETCH_ERROR") or None,
}
with open(os.environ["STATUS_FILE"], "w") as f:
    json.dump(payload, f, indent=2)
    f.write("\n")
PY

log "$HUMAN"

# --- rate-limited notification ------------------------------------------------
# Notify on state change, otherwise at most once every N seconds, so a
# persistently dirty tree does not ping Peter every 5 minutes.
SHOULD_NOTIFY=0
NOW_EPOCH="$(date +%s)"
LAST_STATE=""
LAST_TS=0
if [ -f "$NOTIFY_FILE" ]; then
  LAST_STATE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("state",""))' "$NOTIFY_FILE" 2>/dev/null || echo "")"
  LAST_TS="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("ts",0))' "$NOTIFY_FILE" 2>/dev/null || echo "0")"
fi
case "$LAST_TS" in ''|*[!0-9]*) LAST_TS=0;; esac
if [ "$STATE" != "$LAST_STATE" ]; then
  SHOULD_NOTIFY=1
elif [ $((NOW_EPOCH - LAST_TS)) -ge "$NOTIFY_EVERY_SECONDS" ]; then
  SHOULD_NOTIFY=1
fi

if [ "$SHOULD_NOTIFY" -eq 1 ] && [ "$NO_NOTIFY" != "1" ] && command -v osascript >/dev/null; then
  # Single quotes are stripped from the message: osascript string quoting.
  SAFE_HUMAN="$(printf '%s' "eval-lab sync: $HUMAN" | tr -d "'")"
  osascript -e "display notification \"$SAFE_HUMAN\" with title \"eval-lab sync\"" 2>/dev/null || true
  STATE="$STATE" NOW_EPOCH="$NOW_EPOCH" NOTIFY_FILE="$NOTIFY_FILE" python3 - <<'PY'
import json, os
with open(os.environ["NOTIFY_FILE"], "w") as f:
    json.dump({"state": os.environ["STATE"], "ts": int(os.environ["NOW_EPOCH"])}, f)
PY
elif [ "$SHOULD_NOTIFY" -eq 1 ] && [ "$NO_NOTIFY" = "1" ]; then
  STATE="$STATE" NOW_EPOCH="$NOW_EPOCH" NOTIFY_FILE="$NOTIFY_FILE" python3 - <<'PY'
import json, os
with open(os.environ["NOTIFY_FILE"], "w") as f:
    json.dump({"state": os.environ["STATE"], "ts": int(os.environ["NOW_EPOCH"])}, f)
PY
fi

exit 0
