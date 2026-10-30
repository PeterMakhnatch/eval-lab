#!/bin/bash
# Hardened grader for the MiMo/pytest-hook shape.
# Drop-in for a package's tests/test.sh: keep the package's own M/CWD/V paths and its
# reset list, then grade with cumulative layers selected by HARDEN_LEVEL (0..4).
#
#   L0  unchanged behaviour: reset only the enumerated test files, reward = exit status
#   L1  + auto-load/config reset: every conftest.py, sitecustomize.py, usercustomize.py,
#         pytest.ini, tox.ini, setup.cfg, pyproject.toml, *.pth restored from the base commit
#         (or deleted when absent in base), tracked-modified or untracked
#   L2  + isolated interpreter: PYTHONNOUSERSITE=1 PYTHONSAFEPATH=1, PYTHONPATH/STARTUP/HOME
#         unset, direct pytest rewritten to `python -I -m pytest -p no:cacheprovider`
#   L3  + verified report: --junitxml to a path chosen at grade time; reward 1 only if the
#         report exists, has at least EXPECTED_TESTS tests, and 0 failures/errors/skips
#   L4  + clean-tree grading: rebuild the tree from the base commit and copy in only the
#         agent's changed non-test, non-config source paths; grade there
#
# Environment:
#   M            state dir holding `base` (base commit sha)          default /var/lib/mimo
#   CWD          agent work tree                                      default /testbed
#   V            verifier log dir                                     default /logs/verifier
#   TESTS        read-only tests dir (test.patch, test_command.sh, expected_tests.txt) default /tests
#   HARDEN_LEVEL default 4
# Writes reward.txt only when grading actually ran; a testbed problem exits nonzero with no reward.

set -u
LEVEL="${HARDEN_LEVEL:-4}"
M="${M:-/var/lib/mimo}"
CWD="${CWD:-/testbed}"
V="${V:-/logs/verifier}"
TESTS="${TESTS:-/tests}"
PY="${PY:-python3}"

mkdir -p "$V"
BASE=$(cat "$M/base" 2>/dev/null) || { echo "setup never ran (no $M/base)" >&2; exit 1; }
cd "$CWD" || exit 1
if [ -d "$M/git-hidden" ]; then rm -rf "$CWD/.git"; mv "$M/git-hidden" "$CWD/.git"; fi
# the agent's change against the base, so its own commits count too
git add -A >/dev/null 2>&1 && git -c core.fileMode=false diff --cached "$BASE" > "$V/agent.diff"
git reset -q

is_autoload() {
    case "$1" in
        conftest.py|*/conftest.py|sitecustomize.py|*/sitecustomize.py) return 0 ;;
        usercustomize.py|*/usercustomize.py) return 0 ;;
        pytest.ini|*/pytest.ini|tox.ini|*/tox.ini|setup.cfg|*/setup.cfg) return 0 ;;
        pyproject.toml|*/pyproject.toml|*.pth) return 0 ;;
    esac
    return 1
}

# restore one path from the base commit, or delete it when the base does not have it
restore_one() {
    local f="$1"
    [ -z "$f" ] && return 0
    if git cat-file -e "$BASE:$f" 2>/dev/null; then
        git checkout "$BASE" -- "$f" 2>/dev/null || return 1
    else
        git rm -f --cached --ignore-unmatch -- "$f" >/dev/null 2>&1 || true
        rm -f -- "$f"
    fi
}

reset_autoload() {
    # every auto-load/config path the base knows about
    git ls-tree -r --name-only "$BASE" | while IFS= read -r f; do
        is_autoload "$f" && printf '%s\n' "$f"
    done | while IFS= read -r f; do restore_one "$f"; done
    # every auto-load/config path on disk that the base does not know about
    find . -path ./.git -prune -o -type f -print | sed 's|^\./||' | while IFS= read -r f; do
        if is_autoload "$f" && ! git cat-file -e "$BASE:$f" 2>/dev/null; then rm -f -- "$f"; fi
    done
}

reset_list() {
    while IFS= read -r tf; do
        [ -z "$tf" ] && continue
        restore_one "$tf"
    done <<'EOF_RESET_TEST_FILES'
tests/test_visible.py
mimo_test_command.sh
EOF_RESET_TEST_FILES
}

reset_list || { echo "could not reset the test files (testbed problem, not scored)" >&2; exit 1; }
if [ "$LEVEL" -ge 1 ]; then reset_autoload; fi

if [ "$LEVEL" -ge 2 ]; then
    unset PYTHONPATH PYTHONSTARTUP PYTHONHOME
    export PYTHONNOUSERSITE=1
    export PYTHONSAFEPATH=1
fi

CMD="$(cat "$TESTS/test_command.sh")"
HAS_PYTEST=0
printf '%s' "$CMD" | grep -q -- '-m pytest' && HAS_PYTEST=1
if [ "$LEVEL" -ge 2 ] && [ "$HAS_PYTEST" -eq 1 ]; then
    # the workdir is put back on sys.path explicitly, by a grader-owned launcher, after
    # interpreter start-up: implicit cwd and sitecustomize loading stay closed
    LAUNCHER="$V/pytest_launcher.py"
    cat > "$LAUNCHER" <<'PYLAUNCHER'
import os
import sys

sys.path.insert(0, os.getcwd())

import pytest

sys.exit(pytest.main(sys.argv[1:]))
PYLAUNCHER
    CMD=$(printf '%s' "$CMD" | sed -E 's#(^|[;&|[:space:]])python3?[[:space:]]+-m[[:space:]]+pytest#\1python -I '"$LAUNCHER"'#g')
    CMD="$CMD -p no:cacheprovider"
fi
J="$V/junit-$$-$(od -An -N4 -tu4 /dev/urandom | tr -d ' \n').xml"
JUNIT_REQUIRED="${JUNIT_REQUIRED:-1}"
if [ "$LEVEL" -ge 3 ] && [ "$HAS_PYTEST" -eq 1 ]; then
    CMD="$CMD --junitxml=$J"
elif [ "$LEVEL" -ge 3 ]; then
    # opaque wrapper: pytest may still be the runner inside it, so ask every child pytest for
    # the report through the environment. The gate then applies only when the report appears,
    # unless the package declares JUNIT_REQUIRED=1.
    export PYTEST_ADDOPTS="--junitxml=$J -p no:cacheprovider"
fi
EXPECTED=$(cat "$TESTS/expected_tests.txt" 2>/dev/null || echo 0)
if ! [ "$EXPECTED" -gt 0 ] 2>/dev/null; then
    echo "WARN: $TESTS/expected_tests.txt missing or empty; the JUnit gate cannot check the" \
         "test count and degrades to 'at least one test, none failed/errored/skipped'" >&2
fi

apply_hidden() {
    ( cd "$1" && git apply --verbose "$TESTS/test.patch" > "$V/apply.log" 2>&1 ) || {
        cat "$V/apply.log" >&2
        echo "the hidden tests could not be applied (testbed problem, not scored)" >&2
        return 3
    }
}

# L4: a tree that only contains the base commit plus the agent's declared source changes
build_fresh() {
    local D="$V/fresh"
    rm -rf "$D"; mkdir -p "$D"
    git archive "$BASE" | tar -x -C "$D" || return 1
    git -c core.quotePath=false status --porcelain=v1 --untracked-files=all | while IFS= read -r line; do
        st="${line:0:2}"; p="${line:3}"
        case "$st" in *D*) continue ;; esac
        case "$p" in
            tests/*|test_*|*/test_*|*_test.py|*_test.ts|*_test.js) continue ;;
        esac
        is_autoload "$p" && continue
        [ -f "$p" ] || continue
        mkdir -p "$D/$(dirname "$p")" && cp -p -- "$p" "$D/$p"
    done
    printf '%s' "$D"
}

GATE="GATE_SKIPPED"
if [ "$LEVEL" -ge 4 ]; then
    D=$(build_fresh) || { echo "could not build the clean tree" >&2; exit 1; }
else
    D="$CWD"
fi
apply_hidden "$D" || exit 1
run_command() {
    cd "$D" || return 1
    if command -v timeout >/dev/null 2>&1; then
        timeout 1800 bash -c "$CMD"
    else
        bash -c "$CMD"
    fi
}
run_command > "$V/test_output.log" 2>&1; RC=$?
tail -c 8000 "$V/test_output.log"

GATE="GATE_NO"
if [ "$LEVEL" -ge 3 ] && ! { [ -f "$J" ] || [ "$JUNIT_REQUIRED" = "1" ]; }; then
    GATE="GATE_SKIPPED"
    echo "WARN: no JUnit report and JUNIT_REQUIRED=0: this runner cannot be report-verified;" \
         "grading falls back to the exit status" >&2
elif [ "$LEVEL" -ge 3 ]; then
    GATE=$("$PY" -I - "$J" "$EXPECTED" <<'PYEOF' 2>/dev/null
import sys
import xml.etree.ElementTree as ET

path, expected = sys.argv[1], int(sys.argv[2])
try:
    root = ET.parse(path).getroot()
except Exception:
    print("GATE_NO")
    raise SystemExit(0)
tests = failures = errors = skipped = 0
for case in root.iter("testcase"):
    tests += 1
    failures += case.find("failure") is not None
    errors += case.find("error") is not None
    skipped += case.find("skipped") is not None
ok = tests >= max(expected, 1) and failures == 0 and errors == 0 and skipped == 0
print("GATE_OK" if ok else "GATE_NO")
PYEOF
)
    [ "$GATE" = "GATE_OK" ] || RC=1
fi

if [ $RC -eq 0 ]; then echo 1 > "$V/reward.txt"; else echo 0 > "$V/reward.txt"; fi
echo "test command exited $RC (level $LEVEL, gate $GATE)"
