#!/bin/bash
# install-trace-store-sync.sh — install the private cloud trace-store sync timer.
#
# The timer runs ONE sync tick (`python -m evallab.trace_store.watch`) every
# --interval seconds under launchd StartInterval (RunAtLoad, no KeepAlive). The
# credentials come from the `keys` wrapper at run time; no secret value is ever
# written to the plist, the receipt or a log.
#
# Runtime layout (all outside every worktree, never renamed after the venv build):
#   <state-dir>/releases/<commit>-<unique>/source   git-archive of the committed tree
#   <state-dir>/releases/<commit>-<unique>/venv     locked deps + editable SNAPSHOT source
#   <state-dir>/releases/<commit>-<unique>/INSTALLED receipt (status=ready written last)
# Old releases, sync state and logs are preserved by every operation.
#
# Runtime code and CorpusRegistry authority come from the same committed snapshot.
# The primary checkout is used only to resolve and archive the install revision.
#
# Usage:
#   install-trace-store-sync.sh [--load] [--uninstall] [--label L] [--dest PATH]
#       [--state-dir DIR] [--interval SECONDS] [--db-path PATH]
#       [--sync-state-root DIR] [--repo-root DIR] [--revision REV] [--keys PATH]
#
# --revision (default origin/main) must be merged into the locally fetched
# origin/main. --keys defaults to `command -v keys`. The job is bootstrapped only
# with --load or when this installer's own job was already loaded; otherwise the
# plist is just written.
set -euo pipefail
unset VIRTUAL_ENV PYTHONPATH PYTHONHOME PYTHONSTARTUP || true

ROOT="$(cd "$(dirname "$0")/../../.." && pwd -P)"
OWNER_MARK="evallab-trace-store-sync-installer"
SERVICE_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
DOCENT_PIN="docent==0.1.88"

die() { echo "install-trace-store-sync: $*" >&2; exit 1; }
die2() { echo "install-trace-store-sync: $*" >&2; exit 2; }

LOAD=0
UNINSTALL=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --load) LOAD=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --label|--dest|--state-dir|--interval|--db-path|--sync-state-root|--repo-root|--revision|--keys)
      [ "$#" -ge 2 ] || die2 "$1 requires a value"
      case "$1" in
        --label) LABEL="$2" ;;
        --dest) DEST="$2" ;;
        --state-dir) STATE_DIR="$2" ;;
        --interval) INTERVAL="$2" ;;
        --db-path) DB_PATH="$2" ;;
        --sync-state-root) SYNC_STATE="$2" ;;
        --repo-root) REPO_ROOT="$2" ;;
        --revision) REVISION="$2" ;;
        --keys) KEYS_BIN="$2" ;;
      esac
      shift 2 ;;
    *) die2 "unknown argument: $1" ;;
  esac
done

# ---- validation: nothing below touches the filesystem or launchd ----------------
if [ "$LOAD" -eq 1 ] && [ "$UNINSTALL" -eq 1 ]; then
  die2 "--load and --uninstall are mutually exclusive"
fi
case "${HOME:-}" in /*) ;; *) die2 "HOME must be an absolute path" ;; esac

LABEL="${LABEL-com.petermakhnatch.evallab.trace-store-sync}"
case "$LABEL" in
  ''|*[!A-Za-z0-9._-]*|[.-]*) die2 "invalid --label: $LABEL" ;;
esac
INTERVAL="${INTERVAL-600}"
case "$INTERVAL" in
  ''|*[!0-9]*) die2 "--interval must be a positive integer number of seconds" ;;
esac
[ "${#INTERVAL}" -le 9 ] || die2 "--interval is too large"
INTERVAL=$((10#$INTERVAL))
[ "$INTERVAL" -gt 0 ] || die2 "--interval must be a positive integer number of seconds"

DEST="${DEST-$HOME/Library/LaunchAgents/$LABEL.plist}"
STATE_DIR="${STATE_DIR-$HOME/Library/Application Support/evallab/trace-store-sync}"
DB_PATH="${DB_PATH-$HOME/.local/state/evallab-prod-live/cloud-trace-db.sqlite}"
SYNC_STATE="${SYNC_STATE-$HOME/.local/state/evallab-prod-live/trace-store}"
REVISION="${REVISION-origin/main}"
LOGS="$HOME/Library/Logs/evallab"
DOMAIN="gui/$(id -u)"

for pair in "--dest:$DEST" "--state-dir:$STATE_DIR" "--db-path:$DB_PATH" \
            "--sync-state-root:$SYNC_STATE" "--repo-root:${REPO_ROOT-/}" "--keys:${KEYS_BIN-/}"; do
  case "${pair#*:}" in /*) ;; *) die2 "${pair%%:*} must be an absolute path" ;; esac
done
case "$REVISION" in ''|-*) die2 "invalid --revision: $REVISION" ;; esac

is_own_plist() {
  python3 - "$DEST" "$LABEL" "$OWNER_MARK" <<'PY'
import plistlib
import sys

path, label, mark = sys.argv[1:]
try:
    with open(path, "rb") as handle:
        data = plistlib.load(handle)
except Exception:
    sys.exit(1)
sys.exit(0 if data.get("EvallabManagedBy") == mark and data.get("Label") == label else 1)
PY
}

stop_job() {
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  # bootout returns before launchd drops the job; bootstrapping early fails with EIO (5).
  for _ in $(seq 1 30); do
    launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || return 0
    sleep 1
  done
  die "job $DOMAIN/$LABEL did not unload"
}

# ---- uninstall: own plist/job only; runtimes, state and logs stay -----------------
if [ "$UNINSTALL" -eq 1 ]; then
  if [ ! -e "$DEST" ] && [ ! -L "$DEST" ]; then
    echo "nothing to remove: $DEST does not exist"
    exit 0
  fi
  is_own_plist || die "refusing to uninstall: $DEST is not a plist written by this installer"
  stop_job
  rm -f "$DEST"
  echo "removed: $DEST (runtimes under $STATE_DIR, sync state and logs left in place)"
  exit 0
fi

# ---- install preflight (still read-only) ----------------------------------------
for tool in git uv launchctl python3 tar; do
  command -v "$tool" >/dev/null 2>&1 || die "required tool not found: $tool"
done

if [ -z "${REPO_ROOT+x}" ]; then
  COMMON="$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir)"
  REPO_ROOT="$(cd "$COMMON/.." && pwd -P)"
fi
TOP="$(git -C "$REPO_ROOT" rev-parse --show-toplevel 2>/dev/null)" || die "not a git checkout: $REPO_ROOT"
TOP="$(cd "$TOP" && pwd -P)"
COMMON_DIR="$(git -C "$REPO_ROOT" rev-parse --path-format=absolute --git-common-dir)"
COMMON_DIR="$(cd "$COMMON_DIR" && pwd -P)"
[ "$COMMON_DIR" = "$TOP/.git" ] || die "--repo-root must be the primary checkout (got $REPO_ROOT)"
REPO_ROOT="$TOP"

KEYS_BIN="${KEYS_BIN-$(command -v keys || true)}"
[ -n "$KEYS_BIN" ] || die "keys credential wrapper not found; pass --keys PATH"
case "$KEYS_BIN" in /*) ;; *) die "keys must resolve to an absolute path: $KEYS_BIN" ;; esac
[ -f "$KEYS_BIN" ] && [ -x "$KEYS_BIN" ] || die "keys is not an executable file: $KEYS_BIN"

outside_worktrees() { # label path
  case "$2/" in */.worktrees/*) die "$1 must not live in a worktree: $2" ;; esac
  while IFS= read -r line; do
    case "$line" in
      "worktree "*)
        wt="${line#worktree }"
        case "$2/" in "$wt"/*) die "$1 must not live inside checkout $wt: $2" ;; esac
        ;;
    esac
  done < <(git -C "$REPO_ROOT" worktree list --porcelain)
}
outside_worktrees "--state-dir" "$STATE_DIR"
outside_worktrees "--keys" "$KEYS_BIN"

COMMIT="$(git -C "$REPO_ROOT" rev-parse --verify --quiet "$REVISION^{commit}")" \
  || die "unknown revision: $REVISION"
MAIN="$(git -C "$REPO_ROOT" rev-parse --verify --quiet "refs/remotes/origin/main^{commit}")" \
  || die "origin/main is not fetched locally"
git -C "$REPO_ROOT" merge-base --is-ancestor "$COMMIT" "$MAIN" \
  || die "revision $REVISION ($COMMIT) is not merged into the locally fetched origin/main"

WAS_LOADED=0
if [ -e "$DEST" ] || [ -L "$DEST" ]; then
  is_own_plist || die "refusing to replace $DEST: not a plist written by this installer"
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then WAS_LOADED=1; fi
elif launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  die "job $DOMAIN/$LABEL is loaded but $DEST is not ours; not touching it"
fi

# ---- runtime snapshot ------------------------------------------------------------
RELEASES="$STATE_DIR/releases"
REQ="$(mktemp -t evallab-trace-store-sync-reqs.XXXXXX)"
trap 'rm -f "$REQ"' EXIT

export_requirements() { # source out
  uv export --frozen --no-dev --no-default-groups --no-hashes --no-editable \
    --no-emit-project --extra laminar -o "$2" --project "$1"
}
lmnr_pin() { # requirements file -> pinned lmnr version
  local line
  line="$(grep -m1 '^lmnr==' "$1")" || return 1
  line="${line#lmnr==}"
  echo "${line%%[ ;]*}"
}

# Run inside the venv with a scrubbed environment: the SDK must match the lock and
# evallab must import from the snapshot source, nowhere else.
CHECK_PY="$(cat <<'PY'
import importlib.metadata as metadata
import importlib.util
import pathlib
import sys
from evallab.production.corpus_queue import CorpusRegistry

source = pathlib.Path(sys.argv[1]).resolve()
pin = sys.argv[2]
installed = metadata.version("lmnr")
if installed != pin:
    sys.exit(f"lmnr {installed} installed, lock pins {pin}")
docent = metadata.version("docent")
if docent != "0.1.88":
    sys.exit(f"docent {docent} installed, expected 0.1.88")
spec = importlib.util.find_spec("evallab.trace_store.watch")
if spec is None or spec.origin is None:
    sys.exit("evallab.trace_store.watch is not importable")
origin = pathlib.Path(spec.origin).resolve()
if source not in origin.parents:
    sys.exit(f"evallab imports from {origin}, outside snapshot {source}")
CorpusRegistry(source)
print(f"lmnr={installed} docent={docent} watch={origin} registry={source}")
PY
)"

verify_runtime() { # release pin
  uv pip check --python "$1/venv/bin/python" || return 1
  ( cd "$1/source" && env -i HOME="$HOME" PATH="$SERVICE_PATH" \
      "$1/venv/bin/python" -I -c "$CHECK_PY" "$1/source" "$2" )
}

RELEASE=""
for cand in "$RELEASES/$COMMIT"-*; do
  [ -d "$cand" ] || continue
  [ -f "$cand/INSTALLED" ] && [ -x "$cand/venv/bin/python" ] && [ -d "$cand/source" ] || continue
  grep -qx "status=ready" "$cand/INSTALLED" && grep -qx "commit=$COMMIT" "$cand/INSTALLED" || continue
  export_requirements "$cand/source" "$REQ" >/dev/null 2>&1 || continue
  pin="$(lmnr_pin "$REQ")" || continue
  if verify_runtime "$cand" "$pin" >/dev/null 2>&1; then
    RELEASE="$cand"
    echo "reusing verified runtime snapshot: $RELEASE"
    break
  fi
done

if [ -z "$RELEASE" ]; then
  mkdir -p "$RELEASES"
  # Unique, never reused or overwritten; a failed build stays behind unmarked.
  RELEASE="$(mktemp -d "$RELEASES/$COMMIT-XXXXXX")"
  SOURCE="$RELEASE/source"
  VENV="$RELEASE/venv"
  mkdir "$SOURCE"
  git -C "$REPO_ROOT" archive --format=tar "$COMMIT" | tar -x -C "$SOURCE"
  for needed in pyproject.toml uv.lock src/evallab/trace_store/watch.py; do
    [ -f "$SOURCE/$needed" ] || die "committed snapshot $COMMIT lacks $needed"
  done
  export_requirements "$SOURCE" "$REQ"
  PIN="$(lmnr_pin "$REQ")" || die "uv.lock exports no lmnr pin"
  uv venv --python 3.12 "$VENV"
  uv pip install --python "$VENV/bin/python" -r "$REQ"
  # Both scheduled targets must be installed, without moving any locked pin.
  uv pip install --python "$VENV/bin/python" -c "$REQ" "$DOCENT_PIN"
  # Editable install of the SNAPSHOT (not the caller): evallab resolves repository
  # files relative to its own source.
  uv pip install --python "$VENV/bin/python" --no-deps -e "$SOURCE"
  verify_runtime "$RELEASE" "$PIN"

  RECEIPT="$RELEASE/INSTALLED.tmp"
  {
    echo "status=ready"
    echo "commit=$COMMIT"
    echo "revision=$REVISION"
    echo "installed_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "release=$RELEASE"
    echo "source=$SOURCE"
    echo "venv=$VENV"
    echo "authority=$SOURCE"
    echo "git_repository=$REPO_ROOT"
    echo "lmnr=$PIN"
    echo "docent=$("$VENV/bin/python" -I -c 'import importlib.metadata as m; print(m.version("docent"))')"
    echo "packages:"
    "$VENV/bin/python" -I -c 'import importlib.metadata as m
for line in sorted({d.metadata["Name"] + "==" + d.version for d in m.distributions()}, key=str.lower):
    print(line)'
  } > "$RECEIPT"
  mv "$RECEIPT" "$RELEASE/INSTALLED"
fi
SOURCE="$RELEASE/source"
VENV="$RELEASE/venv"
cp "$RELEASE/INSTALLED" "$STATE_DIR/INSTALLED.tmp"
mv "$STATE_DIR/INSTALLED.tmp" "$STATE_DIR/INSTALLED"
cat "$RELEASE/INSTALLED"

# ---- plist: written only after the new runtime is ready --------------------------
mkdir -p "$LOGS" "$(dirname "$DEST")"
if [ "$WAS_LOADED" -eq 1 ]; then stop_job; fi
python3 - "$DEST" "$LABEL" "$OWNER_MARK" "$KEYS_BIN" "$VENV/bin/python" "$SOURCE" \
  "$DB_PATH" "$SYNC_STATE" "$INTERVAL" "$HOME" "$SERVICE_PATH" "$LOGS" <<'PY'
import os
import plistlib
import sys
from pathlib import Path

(destination, label, mark, keys, python, source, db_path, state_root,
 interval, home, service_path, logs) = sys.argv[1:]
config = {
    "Label": label,
    "EvallabManagedBy": mark,
    "ProgramArguments": [
        keys, "run", "--", python, "-m", "evallab.trace_store.watch",
        "--repo-root", source,
        "--db-path", db_path,
        "--state-root", state_root,
    ],
    "WorkingDirectory": source,
    "RunAtLoad": True,
    "StartInterval": int(interval),
    "ProcessType": "Background",
    "EnvironmentVariables": {"HOME": home, "PATH": service_path},
    "StandardOutPath": str(Path(logs) / "trace-store-sync.log"),
    "StandardErrorPath": str(Path(logs) / "trace-store-sync.error.log"),
}
path = Path(destination)
temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
temporary.write_bytes(plistlib.dumps(config, sort_keys=False))
temporary.replace(path)
print(f"installed: {path}")
print(f"interval: {interval}s")
PY

echo "venv=$VENV"
echo "authority=$SOURCE"
if [ "$LOAD" -eq 1 ] || [ "$WAS_LOADED" -eq 1 ]; then
  launchctl enable "$DOMAIN/$LABEL"
  launchctl bootstrap "$DOMAIN" "$DEST"
  echo "loaded: $DOMAIN/$LABEL"
fi