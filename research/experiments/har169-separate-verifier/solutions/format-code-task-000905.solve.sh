#!/bin/bash
# Oracle reference solution for format-code-task-000905 (Weight negative/high fix).
# Runs in the agent environment; stdout goes to the trial's agent/oracle.txt log.
set -u
cd /testbed

echo "=== separate-verifier agent-env probe ==="
echo "--- ls /tests:"
ls -la /tests 2>&1
echo "--- marker grep (TestWeightNegativeAndHighFix) over workspace:"
grep -rl "TestWeightNegativeAndHighFix" "/testbed" /tmp \
  --exclude-dir=.git --exclude-dir=.venv --exclude-dir=venv \
  --exclude-dir=node_modules 2>/dev/null
echo "grep_rc=$?"

echo "=== oracle fix: Weight.calculate clamps out-of-range sums ==="
python3 - <<'PYEOF'
from pathlib import Path

path = Path("/testbed/quark/utils/weight.py")
src = path.read_text(encoding="utf-8")

old_tail = """        else:
            raise ValueError("Weight calculate failed")
"""
new_tail = """        else:
            if total_weight > level_five_threshold:
                return red(LEVEL_INFO.High.value)
            return green(LEVEL_INFO.LOW.value)
"""
assert src.count(old_tail) == 1, "weight else-branch not found exactly once"
src = src.replace(old_tail, new_tail)
path.write_text(src, encoding="utf-8")
print("patched quark/utils/weight.py")
PYEOF

echo "=== oracle smoke check ==="
python3 - <<'PYEOF'
from quark.utils.weight import Weight

assert Weight(20, -10).calculate().endswith("Low Risk\x1b[0m"), Weight(20, -10).calculate()
assert Weight(20, 100).calculate().endswith("High Risk\x1b[0m")
assert Weight(20, 8).calculate().endswith("Moderate Risk\x1b[0m")
assert Weight(20, 1).calculate().endswith("Low Risk\x1b[0m")
assert Weight(20, 15).calculate().endswith("High Risk\x1b[0m")
print("oracle smoke check passed")
PYEOF
