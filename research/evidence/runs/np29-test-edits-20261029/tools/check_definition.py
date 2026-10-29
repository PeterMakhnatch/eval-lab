"""NP29-CM-1: behaviour checks that the code does what README section 4 says (run: python check_definition.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_edits as te

cases = {
    "pip install -r tests/requirements.txt": (False, False, False),
    "pytest -q > tests/out.log": (False, False, False),
    "pytest -q > tests/out.txt 2>&1": (False, False, False),
    "cd tests && echo x > a_test.py": (True, False, False),
    "cd tests && rm -f a.py": (False, False, True),
    "git checkout HEAD -- tests/test_a.py": (False, False, True),
    "echo x > src/FooTest.java": (True, False, False),
    "echo x >> spec/models/a_spec.rb": (True, True, False),
    "sed -i 's/a/b/' tests/test_a.py": (True, True, False),
    "cat > /tmp/test_x.py <<EOF\nx\nEOF": (False, False, False),
    "git apply <<'EOF'\n--- /dev/null\n+++ b/tests/test_new.py\n@@\n+x\nEOF": (True, False, False),
    "git apply <<'EOF'\n--- a/tests/test_a.py\n+++ b/tests/test_a.py\n@@\n-x\n+y\nEOF": (
        True,
        True,
        False,
    ),
    "apply_patch <<'P'\n*** Begin Patch\n*** Delete File: tests/test_a.py\n*** End Patch\nP": (
        False,
        False,
        True,
    ),
    "apply_patch <<'P'\n*** Begin Patch\n*** Update File: tests/test_a.py\n*** End Patch\nP": (
        True,
        True,
        False,
    ),
    "cp a.py tests/test_b.py": (True, False, False),
}
bad = 0
for cmd, (s, si, sd) in cases.items():
    r = te.analyse_steps(
        [
            {
                "source": "agent",
                "tool_calls": [{"function_name": "bash", "arguments": {"command": cmd}}],
            }
        ]
    )["flags"]
    ok = (r["S"], r["SI"], r["SD"] and not r["S"]) == (s, si, sd)
    if not ok:
        bad += 1
        print("MISMATCH", repr(cmd), r)
print("definition checks: mismatches =", bad)
raise SystemExit(bad)
