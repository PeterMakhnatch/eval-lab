# Shared paths for harbor-021 demos. Source from any demo script.
# Do not set HOME or package-manager homes.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
DEMO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NOTE_DIR="$(cd "${DEMO_DIR}/.." && pwd)"
CAPTURE_DIR="${NOTE_DIR}/captures"
FIXTURE_DIR="${NOTE_DIR}/fixtures"
RUNS_DIR="${ROOT}/runs"

mkdir -p "${CAPTURE_DIR}" "${FIXTURE_DIR}" "${RUNS_DIR}"

# The uv-tool harbor install (0.21.0). Do not use the worktree venv — it has no harbor.
HARBOR_BIN="${HARBOR_BIN:-$(command -v harbor)}"
if [[ -z "${HARBOR_BIN}" ]]; then
  echo "harbor not on PATH" >&2
  exit 127
fi
HARBOR_PY="${HARBOR_PY:-$(head -1 "${HARBOR_BIN}" | sed 's/^#!//')}"

# harbor-atif2otel is a separate package; invoke via uvx, no root lockfile edits.
ATIF2OTEL_PY=(uvx --from harbor-atif2otel python)

harbor_py() {
  "${HARBOR_PY}" "$@"
}

require_docker() {
  if ! docker info >/dev/null 2>&1; then
    echo "Docker is not reachable" >&2
    return 1
  fi
}
