#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf '%s\n' \
    'Usage: scripts/premerge.sh [--full | --static | --focused [TEST_FILE [PYTEST_ARGS...]]]' \
    '  --static   Run static gates only; no tests or runtime smoke.' \
    '  --focused  Run static gates and automatic branch tests, or explicit tests.' \
    '  --full     Reproduce the complete Python 3.12 CI gate (default).'
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
readonly TY_OUTPUT="runs/_premerge/ty.txt"

if [[ "$(uv --version)" != "uv ${UV_VERSION}"* ]]; then
  echo "error: premerge requires uv ${UV_VERSION}; found $(uv --version)" >&2
  exit 1
fi

export UV_PYTHON="${PYTHON_FLOOR}"

# Match the static CI profile before running the same static gates locally.
uv sync --locked --no-group observability --group benchmarks --group lance --group inspect
uv run --no-sync ruff check .
uv run --no-sync python -m evallab.governance check
uv run --no-sync evallab registry audit --json
if [[ "${mode}" != focused || $# -ne 0 ]]; then
  uv run --no-sync python -m evallab.lessons
fi
mkdir -p "$(dirname "${TY_OUTPUT}")"
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
