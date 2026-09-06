#!/usr/bin/env bash
set -euo pipefail

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

# CI test job syncs the benchmark dependency group (fastmcp, cryptography) so the
# live fastmcp/cryptography contract tests run locally instead of skipping via
# pytest.importorskip. Mirror that group to keep premerge a faithful reproduction
# of the combined quality + typecheck workflows (see agents/CHECKS.md).
uv sync --locked --group benchmarks
uv run ruff check .
uv run --no-sync python -m evallab.docindex check
uv run --no-sync python -m evallab.repomap check
uv run python -m evallab.governance check
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
uv run pytest
uv run python -m evallab.smoke --docker-free

echo "premerge green: Python ${PYTHON_FLOOR}; ty 0 <= ${TY_BASELINE}"
