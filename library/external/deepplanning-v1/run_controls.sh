#!/bin/bash
# Linux workflow / workbench controls runner for DeepPlanning canary
set -euo pipefail

TASK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../tasks/experimental/deepplanning-v1/travel-lisbon-002" && pwd)"
echo "=== DeepPlanning Canary Control Runner ==="
echo "Task: ${TASK_DIR}"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "${WORKDIR}"' EXIT

mkdir -p "${WORKDIR}/app/data" "${WORKDIR}/tests" "${WORKDIR}/logs/verifier"
cp "${TASK_DIR}/environment/data/task.json" "${WORKDIR}/app/data/task.json"
cp "${TASK_DIR}/tests/golden.json" "${WORKDIR}/tests/golden.json"
cp "${TASK_DIR}/tests/verify.py" "${WORKDIR}/tests/verify.py"

export APP_ROOT="${WORKDIR}"
export PYTHONDONTWRITEBYTECODE=1

echo "1. Testing Oracle Solution..."
(
  cd "${WORKDIR}"
  python3 "${TASK_DIR}/solution/solve.py"
  python3 "${WORKDIR}/tests/verify.py"
)

echo "2. Testing Fair Alternative..."
(
  cd "${WORKDIR}"
  rm -f "${WORKDIR}/app/answer.json"
  bash "${TASK_DIR}/workbench/fair-alternative.sh"
  python3 "${WORKDIR}/tests/verify.py"
)

echo "3. Testing NOP probe (please-hack.sh)..."
(
  cd "${WORKDIR}"
  rm -f "${WORKDIR}/app/answer.json"
  bash "${TASK_DIR}/workbench/please-hack.sh"
  if python3 "${WORKDIR}/tests/verify.py" 2>/dev/null; then
    echo "ERROR: NOP probe passed verifier!"
    exit 1
  else
    echo "PASS: NOP probe correctly failed verifier."
  fi
)

echo "4. Testing Adversarial Mutants..."
for mutant in "${TASK_DIR}"/workbench/adversarial/*.sh; do
  echo "Running mutant: $(basename "$mutant")..."
  (
    cd "${WORKDIR}"
    bash "$mutant"
    if python3 "${WORKDIR}/tests/verify.py" 2>/dev/null; then
      echo "ERROR: Mutant $(basename "$mutant") passed verifier!"
      exit 1
    else
      echo "PASS: Mutant $(basename "$mutant") correctly failed verifier."
    fi
  )
done

echo "=== All Canary Controls Passed Deterministically ==="
