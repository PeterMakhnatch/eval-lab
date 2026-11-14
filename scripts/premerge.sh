#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf '%s\n' \
    'Usage: scripts/premerge.sh [--full | --static | --focused [TEST_FILE [PYTEST_ARGS...]]]' \
    '  --static   Run static gates only; no tests or runtime smoke.' \
    '  --focused  Run static gates and automatic branch tests, or explicit tests.' \
    '  --full     Reproduce the complete Python 3.12 CI gate (default).' \
    '  Conflict exception: set EVALLAB_PREPUSH_NEEDS_BASE=<sha> and EVALLAB_PREPUSH_REASON=<reason>.'
}

mode=full
case "${1:-}" in
  --focused)
    mode=focused
    shift
    if [[ $# -gt 0 && ( "$1" == -* || ! -f "${1%%::*}" ) ]]; then
      echo "error: --focused expects an existing test file or file::node selector" >&2
      usage >&2
      exit 2
    fi
    ;;
  --static | --full)
    mode="${1#--}"
    shift
    if [[ $# -ne 0 ]]; then
      usage >&2
      exit 2
    fi
    ;;
  -h | --help)
    usage
    exit 0
    ;;
  "")
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

readonly PYTHON_FLOOR="3.12"
readonly UV_VERSION="0.9.24"
readonly TY_VERSION="0.0.71"
readonly TY_BASELINE="0"

if [[ "$(uv --version)" != "uv ${UV_VERSION}"* ]]; then
  echo "error: premerge requires uv ${UV_VERSION}; found $(uv --version)" >&2
  exit 1
fi

export UV_PYTHON="${PYTHON_FLOOR}"

# Match the static CI profile before running the same static gates locally.
uv sync --locked --no-group observability --group benchmarks --group inspect
uv run --no-sync ruff check .
uv run --no-sync python -m evallab.governance check
uv run --no-sync evallab registry audit --json
if [[ "${mode}" != focused ]]; then
  uv run --no-sync python -m evallab.lessons
fi
# Typecheck output is ephemeral tool scratch, not retained Harbor run evidence.
# Use a private file outside the checkout, including when TMPDIR points into it.
TY_OUTPUT="$(mktemp /tmp/evallab-premerge-ty.XXXXXX)"
readonly TY_OUTPUT
trap 'rm -f -- "${TY_OUTPUT}"' EXIT
set +e
uvx "ty@${TY_VERSION}" check src/ --output-format=concise > "${TY_OUTPUT}" 2>&1
ty_exit=$?
set -e
cat "${TY_OUTPUT}"

if [[ "${ty_exit}" -ne 0 ]]; then
  echo "error: ty check failed (exit ${ty_exit}); see output above" >&2
  exit "${ty_exit}"
fi
case "${mode}" in
  static)
    echo "static checks passed: Python ${PYTHON_FLOOR}; no tests or runtime smoke run; not CI green"
    ;;
  focused)
    echo "local prepush checkpoint: selected coverage only; not CI green"
    branch="$(git symbolic-ref --quiet --short HEAD)"
    echo "push check branch: ${branch}"
    push_args=()
    if [[ -n "${EVALLAB_PREPUSH_NEEDS_BASE:-}" || -n "${EVALLAB_PREPUSH_REASON:-}" ]]; then
      if [[ -z "${EVALLAB_PREPUSH_NEEDS_BASE:-}" || -z "${EVALLAB_PREPUSH_REASON:-}" ]]; then
        echo "error: set both EVALLAB_PREPUSH_NEEDS_BASE and EVALLAB_PREPUSH_REASON" >&2
        exit 2
      fi
      push_args=(--needs-base "${EVALLAB_PREPUSH_NEEDS_BASE}" --reason "${EVALLAB_PREPUSH_REASON}")
    fi
    if ! git diff --quiet HEAD --; then
      echo "push check skipped: uncommitted tracked changes; check-push compares committed HEAD and could falsely refuse the same patch."
    else
      push_exit=0
      push_output="$(uv run --no-sync evallab ship check-push "${push_args[@]}" 2>&1)" || push_exit=$?
      printf '%s\n' "${push_output}"
      if [[ "${push_exit}" -ne 0 ]]; then
        case "${push_output}" in
          "Command refused ("*"): gh "* | "Command refused ("*"): git fetch "*)
            echo "warning: remote push check unavailable; continuing offline local checks; rerun ship check-push before pushing." >&2
            ;;
          *) exit "${push_exit}" ;;
        esac
      fi
    fi
    if [[ $# -eq 0 ]]; then
      uv run --no-sync pytest --collect-only -q -n 0 -p no:cacheprovider
      uv run --no-sync evallab registry devloop \
        --since "${EVALLAB_PREPUSH_BASE:-origin/main}" --checkpoint --run
    else
      uv run --no-sync pytest -n 0 -p no:cacheprovider "$@"
    fi
    echo "focused checks passed: Python ${PYTHON_FLOOR}; full suite and runtime smoke not run; not CI green"
    ;;
  full)
    uv run --no-sync pytest
    uv run --no-sync python -m evallab.smoke --docker-free
    echo "premerge green: Python ${PYTHON_FLOOR}; ty 0 <= ${TY_BASELINE}"
    ;;
esac
