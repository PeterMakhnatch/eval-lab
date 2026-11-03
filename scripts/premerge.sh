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
# The files CI's "Native Harbor diff, regrade and observability contracts" step
# runs after installing the laminar extra (.github/workflows/ci.yml). They skip in
# the main suite because Harbor is absent there. tests/test_quality_gates.py fails
# when this list and ci.yml's differ (HAR-264); change both together.
readonly -a HARBOR_NATIVE_TESTS=(
  tests/test_job_diff.py tests/test_regrade.py
  tests/test_harbor_laminar.py tests/test_mimoagent_worker.py
  tests/test_mimoagent_stops.py tests/test_mimoagent_trajectory.py
  tests/test_execution_contracts.py
  tests/test_native_telemetry.py tests/test_credential_bridge.py
  tests/test_harbor_docker.py tests/test_harbor_modal.py tests/test_egress_lock.py
  tests/test_ref_subset_modal.py tests/test_qa_task_modal.py tests/test_upstream_grader.py
  tests/test_heldout_v7_native.py tests/test_phase2_native_generation.py
  tests/test_candidate_runner.py tests/test_candidate_port.py
  tests/test_harbor_trace_inputs.py
  tests/test_harbor_host_isolation.py tests/test_host_isolation_compose.py
  tests/test_agent_network_policy.py
  tests/test_cloud_trace_export.py tests/test_harbor_analyze_reader.py
  tests/test_heldout_v7_rounds.py tests/test_campaign_execution.py
  tests/test_mimoagent_clean_options.py tests/test_harbor024_agent_kwargs.py
  tests/test_auditor_sft_data.py tests/test_antigravity_bridge.py
  tests/test_task_workbench.py tests/test_tb4_fingerprints.py
)

if [[ "$(uv --version)" != "uv ${UV_VERSION}"* ]]; then
  echo "error: premerge requires uv ${UV_VERSION}; found $(uv --version)" >&2
  exit 1
fi

export UV_PYTHON="${PYTHON_FLOOR}"

# The CI test job syncs the benchmark (fastmcp, cryptography) and lance groups so
# their contract tests run instead of skipping via pytest.importorskip. Mirror them
# to keep premerge a faithful reproduction of the combined quality + typecheck
# workflows (see agents/CHECKS.md). `observability` stays opt-in everywhere.
uv sync --locked --group benchmarks --group lance
uv run --no-sync ruff check .
uv run --no-sync python -m evallab.docindex check
uv run --no-sync python -m evallab.repomap check
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
    # CI order: the main suite and smoke run without Harbor so the skip paths stay
    # faithful; only then does the native Harbor step install the laminar extra.
    # The next premerge sync drops the extra again.
    uv run --no-sync pytest
    uv run --no-sync python -m evallab.smoke --docker-free
    uv sync --locked --no-group observability --group benchmarks --group lance --extra laminar
    uv run --no-sync python -c 'import harbor.job_diff'
    uv run --no-sync pytest -n 0 -rA "${HARBOR_NATIVE_TESTS[@]}"
    echo "premerge green: Python ${PYTHON_FLOOR}; ty 0 <= ${TY_BASELINE}; native Harbor step passed"
    ;;
esac
