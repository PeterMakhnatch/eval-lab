#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf '%s\n' \
    'Usage: scripts/premerge.sh [--full | --static | --focused TEST_FILE [PYTEST_ARGS...]]' \
    '  --static   Run static gates only; no tests or runtime smoke.' \
    '  --focused  Run static gates and explicitly selected pytest tests.' \
    '  --full     Reproduce the complete Python 3.12 CI gate (default).'
}

mode=full
case "${1:-}" in
  --focused)
    mode=focused
    shift
    if [[ $# -eq 0 || "$1" == -* || ! -f "${1%%::*}" ]]; then
      echo "error: --focused requires an existing test file or file::node selector first" >&2
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

# Match the static CI profile; install native runtimes after type checking.
uv sync --locked --no-group observability --group benchmarks --group lance --group inspect
uv run --no-sync ruff check .
uv run --no-sync python -m evallab.governance check
uv run --no-sync evallab registry audit --json
uv run --no-sync python -m evallab.lessons
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
    uv run --no-sync pytest "$@"
    echo "focused checks passed: Python ${PYTHON_FLOOR}; full suite and runtime smoke not run; not CI green"
    ;;
  full)
    uv sync --project tools/mimoagent-harbor --locked --python 3.12
    uv sync --locked --no-group observability --group benchmarks --group lance --group inspect --extra laminar
    uv run --no-sync python -c 'import harbor.job_diff'
    uv run --no-sync pytest --junitxml=runs/_premerge/full-suite.xml
    uv run --no-sync python scripts/ci/check_harbor_skips.py runs/_premerge/full-suite.xml
    uv run --no-sync python -m evallab.smoke --docker-free
    echo "premerge green: Python ${PYTHON_FLOOR}; ty 0 <= ${TY_BASELINE}"
    ;;
esac
