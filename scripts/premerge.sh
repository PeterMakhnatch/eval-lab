#!/usr/bin/env bash
set -euo pipefail

readonly PYTHON_FLOOR="3.12"
readonly UV_VERSION="0.9.24"
readonly TY_VERSION="0.0.71"
readonly TY_BASELINE="33"
readonly TY_OUTPUT="runs/_premerge/ty.txt"

if [[ "$(uv --version)" != "uv ${UV_VERSION}"* ]]; then
  echo "error: premerge requires uv ${UV_VERSION}; found $(uv --version)" >&2
  exit 1
fi

export UV_PYTHON="${PYTHON_FLOOR}"

uv sync --locked
uv run ruff check .
uv run pytest

mkdir -p "$(dirname "${TY_OUTPUT}")"
uvx "ty@${TY_VERSION}" check src/ --output-format=concise > "${TY_OUTPUT}" 2>&1 || true
cat "${TY_OUTPUT}"

count="$(grep -oE 'Found [0-9]+ diagnostic' "${TY_OUTPUT}" | grep -oE '[0-9]+' || echo 0)"
if [[ "${count}" -gt "${TY_BASELINE}" ]]; then
  echo "error: ty reports ${count} diagnostics, above baseline ${TY_BASELINE}" >&2
  exit 1
fi

if [[ "${count}" -lt "${TY_BASELINE}" ]]; then
  echo "notice: ty is down to ${count}; lower the baseline from ${TY_BASELINE}"
fi

echo "premerge green: Python ${PYTHON_FLOOR}; ty ${count} <= ${TY_BASELINE}"
