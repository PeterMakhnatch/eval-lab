"""``grader-hardening@1``: auto-load reset (L1) and JUnit report gate (L3) for single-container MiMo graders.

The upstream MiMo ``tests/test.sh`` resets an *enumerated* list of test files, runs the hidden test command in the
agent's own container and writes ``reward.txt`` from the command's exit status. Two cheap, tested layers close the
routes that need only the ability to create files (ds-tq2-hook-hardening-20261030, executed validation in
research/experiments/h1-grader-hardening-20261030):

* **L1** restores or deletes every auto-load/config file *by pattern over the tree* (``conftest.py``,
  ``sitecustomize.py``, ``usercustomize.py``, ``pytest.ini``, ``tox.ini``, ``setup.cfg``, ``pyproject.toml``,
  ``*.pth``) after the enumerated reset and before the hidden patch is applied, so a hidden test's own ``conftest.py``
  still lands. Untracked or ignored files under dependency directories (``node_modules``, virtualenvs,
  ``site-packages``) are left alone, and every deletion is logged to ``$V/l1-removed.txt``.
* **L3** asks every pytest process for a ``--junitxml`` report (``PYTEST_ADDOPTS``, because the real command is an
  opaque ``mimo_test_command.sh`` wrapper) at a path chosen at grade time, and gives reward 1 only if the command exits
  0 *and* the report exists, holds at least the reference test count and has no failure, error or extra skip. A
  missing report fails closed.

Not closed by this transform, by design: code running in the grader's own interpreter or environment (a ``.pth`` in
a writable site directory that forges the report) needs a separate verifier container; a runner that clears
``PYTEST_ADDOPTS`` (see ``separate_verifier``) defeats L3; plugin entry points installed after setup are not reset.

The reference count comes from a real oracle run of the package and is stored in ``tests/expected_report.json``. The
transform refuses to build without it unless ``allow_degraded`` is set, so a gate that accepts "one passing test" is
never produced silently. Packages whose runner is not pytest get L1 only, and only with ``allow_no_report_gate``.
"""

from __future__ import annotations

import base64
import io
import re
import tarfile

from evallab.task_variants import VariantInvalid

TEST_SH_REL = "tests/test.sh"

AUTOLOAD_PATTERN = (
    "conftest.py|*/conftest.py|sitecustomize.py|*/sitecustomize.py|usercustomize.py|*/usercustomize.py|"
    "pytest.ini|*/pytest.ini|tox.ini|*/tox.ini|setup.cfg|*/setup.cfg|pyproject.toml|*/pyproject.toml|*.pth"
)

_RESET_END = (
    '); then echo "could not reset the test files (testbed problem, not scored)" >&2; exit 1; fi\n'
)
_RUN_LINE = 'timeout 1800 sh -c "$(cat /tests/test_command.sh)" > "$V/test_output.log" 2>&1\n'
_REWARD_LINE = (
    'if [ $RC -eq 0 ]; then echo 1 > "$V/reward.txt"; else echo 0 > "$V/reward.txt"; fi\n'
)
_FINAL_ECHO = 'echo "test command exited $RC"\n'


def l1_block(pattern: str = AUTOLOAD_PATTERN) -> str:
    """The L1 shell block resetting every file whose path matches the ``case`` ``pattern``."""
    return f"""\
# grader-hardening@1 L1: reset every auto-load/config file by pattern, not by the enumerated list above
l1_is_autoload() {{ case "$1" in {pattern}) return 0 ;; esac; return 1; }}
# pyproject.toml and setup.cfg also carry dependencies and metadata that a correct fix may change (4 of 567 vendored
# references do): they are reset only when the agent's change to them mentions pytest, addopts or plugins
l1_is_soft_config() {{ case "$1" in pyproject.toml|*/pyproject.toml|setup.cfg|*/setup.cfg) return 0 ;; esac; return 1; }}
l1_runner_config_changed() {{ git diff "$BASE" -- "$1" | grep -E '^[+-]' | grep -vE '^(\\+\\+\\+|---)' | grep -qiE 'pytest|addopts|plugins'; }}
l1_in_dependency_dir() {{
  case "/$1" in */node_modules/*|*/.venv/*|*/venv/*|*/site-packages/*|*/.tox/*|*/.eggs/*|*/.nox/*|*/__pypackages__/*) return 0 ;; esac
  return 1
}}
: > "$V/l1-removed.txt"
if ! (
  git ls-tree -r --name-only "$BASE" | while IFS= read -r f; do
    l1_is_autoload "$f" || continue
    if l1_is_soft_config "$f" && ! l1_runner_config_changed "$f"; then continue; fi
    git checkout "$BASE" -- "$f" || exit 1
  done
  {{ git ls-files --others --exclude-standard; git ls-files --others --ignored --exclude-standard; }} | sort -u | while IFS= read -r f; do
    l1_is_autoload "$f" || continue
    l1_in_dependency_dir "$f" && continue
    git cat-file -e "$BASE:$f" 2>/dev/null && continue
    rm -f -- "$f" && echo "$f" >> "$V/l1-removed.txt"
  done
); then echo "could not reset the auto-load files (testbed problem, not scored)" >&2; exit 1; fi
"""


_L3_BEFORE = """\
# grader-hardening@1 L3: every pytest process writes a JUnit report to a path chosen now; reward needs the report
J="$V/junit-$$-$(od -An -N4 -tu4 /dev/urandom | tr -d ' \\n').xml"
export PYTEST_ADDOPTS="${PYTEST_ADDOPTS:+$PYTEST_ADDOPTS }--junitxml=$J"
"""

_L3_AFTER = """\
GATE=GATE_NO
if [ -f "$J" ]; then
  GATE=$(python3 -I - "$J" "$EXPECTED_JSON" <<'PYEOF' 2>/dev/null
import json, sys
import xml.etree.ElementTree as ET
path, expected = sys.argv[1], sys.argv[2]
try:
    root = ET.parse(path).getroot()
    want = json.load(open(expected))
except Exception:
    print("GATE_NO")
    raise SystemExit(0)
tests = failures = errors = skipped = 0
for case in root.iter("testcase"):
    tests += 1
    failures += case.find("failure") is not None
    errors += case.find("error") is not None
    skipped += case.find("skipped") is not None
ok = tests >= max(int(want.get("tests", 0)), 1) and failures == 0 and errors == 0 and skipped <= int(want.get("skipped", 0))
print("GATE_OK" if ok else "GATE_NO")
PYEOF
  )
fi
[ "$GATE" = GATE_OK ] || RC=1
"""


def _replace_once(text: str, old: str, new: str, what: str) -> str:
    if text.count(old) != 1:
        raise VariantInvalid(f"parent {TEST_SH_REL} does not have the expected {what} exactly once")
    return text.replace(old, new, 1)


def build_env_command(patch_text: str) -> str:
    """Text of the ``test_command.sh`` inside the base64 ``mimo_build_env`` tarball the hidden patch ships, if any."""
    blob: list[str] = []
    capture = False
    for line in patch_text.splitlines():
        header = re.match(r"diff --git a/(\S+) b/\S+", line)
        if header:
            capture = "build_env.tar.gz.b64" in header.group(1)
        elif capture and line.startswith("+") and not line.startswith("+++"):
            blob.append(line[1:].strip())
    if not blob:
        return ""
    try:
        with tarfile.open(
            fileobj=io.BytesIO(base64.b64decode("".join(blob))), mode="r:gz"
        ) as archive:
            return "\n".join(
                member_file.read().decode("utf-8", "replace")
                for member in archive.getmembers()
                if member.isfile() and member.name.endswith("test_command.sh")
                if (member_file := archive.extractfile(member)) is not None
            )
    except (tarfile.TarError, ValueError, OSError):
        return ""


def build_test_sh(
    parent_text: str, *, with_report_gate: bool, l1_pattern: str = AUTOLOAD_PATTERN
) -> str:
    """Parent ``tests/test.sh`` plus L1 (resetting ``l1_pattern``) and, when ``with_report_gate``, L3."""
    if "grader-hardening@1" in parent_text:
        raise VariantInvalid("parent is already grader-hardened")
    text = _replace_once(
        parent_text, _RESET_END, _RESET_END + l1_block(l1_pattern), "reset-failure line"
    )
    if with_report_gate:
        text = _replace_once(text, _RUN_LINE, _L3_BEFORE + _RUN_LINE, "test command line")
        text = _replace_once(
            text,
            _REWARD_LINE,
            "EXPECTED_JSON=/tests/expected_report.json\n" + _L3_AFTER + _REWARD_LINE,
            "reward line",
        )
        text = _replace_once(
            text, _FINAL_ECHO, 'echo "test command exited $RC (gate $GATE)"\n', "final echo"
        )
    return text


