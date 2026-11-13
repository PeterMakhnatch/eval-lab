#!/bin/bash
# separate-verifier@3 entry: patch-only grading in a pristine verifier checkout.
# Extends @2 with multi-runner drops, cross-language tamper gates, and
# structured per-runner grading (exit-code fallback for silent families).
# Exit 0 whenever grading completes (reward in /logs/verifier/reward.txt);
# exit 1 only for testbed problems (setup/patch failures: not scored).
set -u
SNAP="/var/tmp/mimo-separate"
M="/var/lib/mimo"
CWD="@@WORKDIR@@"
V=/logs/verifier
GRADE=/var/lib/mimo-grade
TS_BAK="$GRADE/tests"
mkdir -p "$V" "$GRADE"
mkdir -p "$GRADE/v10shim" "$GRADE/pycache"
echo "@@V10_UNITTEST_SHIM_B64@@" | base64 -d > "$GRADE/v10shim/sitecustomize.py"
# 0. Pristine checkout: rerun the bundled clean setup (same as @2).
rm -rf "$TS_BAK"; cp -a /tests "$TS_BAK"
mkdir -p "$M"; cp -r "$TS_BAK/_verifier-setup/." "$M/"
if ! bash "$M/setup.sh" > "$GRADE/setup.log" 2>&1; then
  echo "verifier setup failed (testbed problem, not scored)" >&2; exit 1
fi
rm -rf /tests; cp -a "$TS_BAK" /tests; rm -rf "$TS_BAK"
mkdir -p "$V"; cp "$GRADE/setup.log" "$V/setup.log"
BASE=$(cat "$M/base" 2>/dev/null) || { echo "verifier setup never ran (no $M/base)" >&2; exit 1; }
cd "$CWD" || exit 1
if [ -d "$M/git-hidden" ]; then rm -rf "$CWD/.git"; mv "$M/git-hidden" "$CWD/.git"; fi
test -f /tests/test.patch || { echo "bundled test.patch missing (testbed problem)" >&2; exit 1; }
test -f /tests/test_command.sh || { echo "bundled test_command.sh missing (testbed)" >&2; exit 1; }
# @10 repeat-idempotent pristine: restore the anchored first-run tree
# before capturing, so reruns never capture run-0 leftovers as pristine.
if [ -f "$M/pristine-tree" ]; then
  _V10_ANCHORED=$(cat "$M/pristine-tree")
  rm -rf "$GRADE/junit-phases" "$GRADE/baseline" "$GRADE/build_env" "$GRADE/pycache"
  rm -f "$GRADE/junit.xml" "$GRADE/unittest-junit.xml" "$GRADE/integrity.txt" "$GRADE/integrity-unittest.txt" "$GRADE/hook_installed" "$V/timing.log"
  export GIT_INDEX_FILE=/tmp/pristine.index; rm -f "$GIT_INDEX_FILE"
  git read-tree "$_V10_ANCHORED" || { echo "anchored pristine read failed" >&2; exit 1; }
  git checkout-index -f -a || { echo "anchored pristine restore failed" >&2; exit 1; }
  git clean -fdq || { echo "anchored pristine clean failed" >&2; exit 1; }
  git ls-files --others --ignored --exclude-standard -z > /tmp/post_ignored.bin 2>/dev/null || : > /tmp/post_ignored.bin
  python3 - "$M/pristine-ignored.bin" /tmp/post_ignored.bin <<'PYEOF_V10_CLEAN' 2>/dev/null || true
import os, shutil, sys
pre = set(filter(None, open(sys.argv[1], "rb").read().split(b"\0"))) if os.path.exists(sys.argv[1]) else set()
post = set(filter(None, open(sys.argv[2], "rb").read().split(b"\0"))) if os.path.exists(sys.argv[2]) else set()
for raw in post - pre:
    p = os.fsdecode(raw)
    if os.path.isdir(p) and not os.path.islink(p):
        shutil.rmtree(p, ignore_errors=True)
    elif os.path.lexists(p):
        os.unlink(p)
    d = os.path.dirname(p)
    while d and d != ".":
        try:
            os.rmdir(d)
        except OSError:
            break
        d = os.path.dirname(d)
PYEOF_V10_CLEAN
  unset GIT_INDEX_FILE
fi
# Capture tracked and untracked non-ignored files before importing agent bytes
# (matches render_snapshot_hook_v2's git ls-files -c -o --exclude-standard).
export GIT_INDEX_FILE=/tmp/pristine.index; rm -f "$GIT_INDEX_FILE"
git read-tree "$BASE" || { echo "pristine-tree read failed" >&2; exit 1; }
git add -A . || { echo "pristine-tree capture failed" >&2; exit 1; }
PRISTINE=$(git write-tree) || { echo "pristine-tree write failed" >&2; exit 1; }
git diff --cached --name-only "$BASE" > "$V/pristine-extra-files.log"
unset GIT_INDEX_FILE
if [ ! -f "$M/pristine-tree" ]; then
  git ls-files --others --ignored --exclude-standard -z > "$M/pristine-ignored.bin" 2>/dev/null || : > "$M/pristine-ignored.bin"
  printf '%s\n' "$PRISTINE" > "$M/pristine-tree" || { echo "pristine anchor write failed" >&2; exit 1; }
fi
# @6 baseline: hidden tests on the pristine tree (no agent bytes), for the
# fail-to-pass rule. Afterwards the tree returns to exactly $PRISTINE.
export GIT_INDEX_FILE=/tmp/baseline.index; rm -f "$GIT_INDEX_FILE"
git read-tree "$PRISTINE" || { echo "baseline index failed" >&2; exit 1; }
git ls-files --others --ignored --exclude-standard -z > "$V/pre_ignored.bin" 2>/dev/null || : > "$V/pre_ignored.bin"
BTESTFILES=$(grep '^diff --git' /tests/test.patch | sed 's#.* b/##')
printf '%s\n' "$BTESTFILES" | while IFS= read -r tf; do
  [ -z "$tf" ] && continue
  if git cat-file -e "$PRISTINE:$tf" 2>/dev/null; then
    git checkout -q "$PRISTINE" -- "$tf" 2>/dev/null || true
  else
    git rm -f --cached "$tf" >/dev/null 2>&1 || true
    rm -f "$tf"
  fi
done
if git apply --verbose /tests/test.patch > "$V/baseline_apply.log" 2>&1; then
  RES_B=$(cat /tests/test_command.sh)
  for ref in $(grep -oE '[^ "]*mimo_test_command\.sh' /tests/test_command.sh | sort -u); do
    base=${ref##*/}
    if [ -f "$base" ]; then RES_B="$RES_B
$(cat "$base")"; fi
  done
  if printf '%s' "$RES_B" | grep -q 'build_env/test_command'; then
    if [ -f mimo_build_env.tar.gz.b64 ]; then
      rm -rf "$GRADE/build_env" && mkdir -p "$GRADE/build_env"
      if base64 -d mimo_build_env.tar.gz.b64 2>/dev/null | tar -xzf - -C "$GRADE/build_env" 2>/dev/null; then
        BE_TC=$(find "$GRADE/build_env" -name 'test_command.sh' | head -1)
        if [ -n "$BE_TC" ]; then RES_B="$RES_B
$(cat "$BE_TC")"; fi
      fi
    fi
  fi
  printf '%s' "$RES_B" > "$V/baseline_resolved.txt"
  RUNNER_B=custom
  if printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])go +test( |$)'; then RUNNER_B=go-test
  elif printf '%s' "$RES_B" | grep -qE 'GO_BIN" +test( |$)'; then RUNNER_B=go-test
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])jest([^[:alnum:]_]|$)'; then RUNNER_B=jest
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])vitest([^[:alnum:]_]|$)'; then RUNNER_B=vitest
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])_?mocha([^[:alnum:]_]|$)'; then RUNNER_B=mocha
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])ava([^[:alnum:]_]|$)'; then RUNNER_B=ava
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])tap([^[:alnum:]_]|$)'; then RUNNER_B=tap
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])karma([^[:alnum:]_]|$)'; then RUNNER_B=karma
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])jasmine([^[:alnum:]_]|$)'; then RUNNER_B=jasmine
  elif printf '%s' "$RES_B" | grep -qE 'node +--test([^[:alnum:]_]|$)'; then RUNNER_B=node-test
  elif printf '%s' "$RES_B" | grep -qE 'cargo +test([^[:alnum:]_]|$)'; then RUNNER_B=cargo-test
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])rspec([^[:alnum:]_]|$)'; then RUNNER_B=rspec
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])phpunit([^[:alnum:]_]|$)'; then RUNNER_B=phpunit
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])mvn([^[:alnum:]_]|$)|surefire|failsafe'; then RUNNER_B=mvn
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])gradlew?([^[:alnum:]_]|$)'; then RUNNER_B=gradle
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])pytest([^[:alnum:]_]|$)'; then RUNNER_B=pytest
  elif printf '%s' "$RES_B" | grep -qE '\-m +unittest([^[:alnum:]_]|$)'; then RUNNER_B=unittest
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])forge +test([^[:alnum:]_]|$)'; then RUNNER_B=forge
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])node([^[:alnum:]_]|$)'; then RUNNER_B=node-run
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])(make|ctest|cmake)([^[:alnum:]_]|$)'; then RUNNER_B=make
  elif printf '%s' "$RES_B" | grep -qE '(^|[^[:alnum:]_])bats([^[:alnum:]_]|$)'; then RUNNER_B=bats
  fi
  echo "RUNNER_B=$RUNNER_B" > "$V/baseline_runner.txt"
  : > "$V/baseline.log"
  if [ "$RUNNER_B" = pytest ]; then
    CLEARED_B=0
    printf '%s' "$RES_B" | grep -q 'unset.*PYTEST_ADDOPTS' && CLEARED_B=1
    printf '%s' "$RES_B" | grep -q 'PYTEST_ADDOPTS=' && CLEARED_B=1
    printf '%s' "$RES_B" | grep -qE 'env +([^ ]+ +)*-u([^ ]* +)*PYTEST_ADDOPTS|env +-u +PYTEST_ADDOPTS' && CLEARED_B=1
    if [ "$CLEARED_B" = 1 ]; then
      echo "@@V3_CONFTEST_HOOK_B64@@" | base64 -d >> ./conftest.py
      echo "installed verifier conftest hook (addopts cleared)" >> "$V/baseline.log"
    fi
    python3 - "$RES_B" "$CWD" > "$V/baseline-conftest-targets.txt" 2>/dev/null <<'PYEOF_BASELINE' || true
import os, re, sys
resolved, cwd = sys.argv[1], sys.argv[2]
seen = []
for tok in re.findall(r'[A-Za-z0-9_./-]+\.py', resolved):
    rel = tok
    if os.path.isabs(rel):
        try:
            rel = os.path.relpath(rel, cwd)
        except ValueError:
            continue
    if rel.startswith(".."):
        continue
    if os.path.isfile(os.path.join(cwd, rel)):
        d = os.path.dirname(rel) or "."
        if d not in seen:
            seen.append(d)
for d in ["."] + seen:
    print(d)
PYEOF_BASELINE
    while IFS= read -r _v5_d; do
      [ -n "$_v5_d" ] || continue
      _v5_cf="$CWD/$_v5_d/conftest.py"
      mkdir -p "$(dirname "$_v5_cf")"
      if ! grep -q "mimo-junit-archive/1" "$_v5_cf" 2>/dev/null; then
        echo "@@V5_ARCHIVE_HOOK_B64@@" | base64 -d >> "$_v5_cf"
      fi
    done < "$V/baseline-conftest-targets.txt"
    echo "installed verifier junit archive hook" >> "$V/baseline.log"
    # The baseline runs to completion: -x/--exitfirst/--maxfail (from the
    # command, ini, or env) would truncate it at the first failure and hide
    # later skips/failures from fail-to-pass (measured: 000203 oracle
    # false-0). Neutralize maxfail via a wrapping pytest_configure hook
    # (previous same-file hook still runs); the reset below removes it, so
    # the agent path keeps the original command.
    while IFS= read -r _v6_d; do
      [ -n "$_v6_d" ] || continue
      _v6_cf="$CWD/$_v6_d/conftest.py"
      if ! grep -q "mimo-baseline-noexitfirst/1" "$_v6_cf" 2>/dev/null; then
        cat >> "$_v6_cf" <<'PYEOF_NOEXITFIRST' || true
# mimo-baseline-noexitfirst/1: run the pristine baseline to completion.
_prev_baseline_configure = globals().get("pytest_configure")
def pytest_configure(config):
    try:
        if _prev_baseline_configure is not None:
            _prev_baseline_configure(config)
    except Exception:
        pass
    try:
        if getattr(config.option, "maxfail", 0):
            config.option.maxfail = 1000000
    except Exception:
        pass
PYEOF_NOEXITFIRST
      fi
    done < "$V/baseline-conftest-targets.txt"
    echo "installed baseline noexitfirst hook" >> "$V/baseline.log"
  fi
  if [ "$RUNNER_B" = pytest ] || [ "$RUNNER_B" = unittest ]; then
    JUNIT_B=$GRADE/baseline/junit.xml; mkdir -p "${JUNIT_B%/*}" "$GRADE/baseline/junit-phases"
    START_B=$(date +%s)
    _V7_PHASES=$(grep -oE '(^|[^[:alnum:]_])(python[0-9.]* -m (pytest|unittest)|pytest)([^[:alnum:]_]|$)' "$V/baseline_resolved.txt" | wc -l)
    if [ "$_V7_PHASES" -ge 2 ] && command -v bash >/dev/null 2>&1; then
      cat > "$V/baseline-noerrexit.sh" <<'BASELINE_NOERREXIT_EOF'
# @7 baseline: hold errexit off so every test phase runs on pristine.
set -T 2>/dev/null || true
shopt -s extdebug 2>/dev/null || true
trap 'set +e 2>/dev/null || true' DEBUG 2>/dev/null || true
BASELINE_NOERREXIT_EOF
      BASH_ENV="$V/baseline-noerrexit.sh" PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX="$GRADE/pycache" PYTHONPATH="$GRADE/v10shim${PYTHONPATH:+:$PYTHONPATH}" MIMO_VERIFIER_UNITTEST_JUNIT="$GRADE/baseline/unittest-junit.xml" MIMO_VERIFIER_INTEGRITY_DIR="$GRADE/baseline" MIMO_VERIFIER_JUNIT="$GRADE/baseline/junit.xml" MIMO_VERIFIER_ARCHIVE_DIR="$GRADE/baseline/junit-phases" PYTHONUNBUFFERED=1 PYTEST_ADDOPTS="--junitxml=$JUNIT_B -p no:cacheprovider" timeout 1800 bash -c "$(cat /tests/test_command.sh)" > "$V/baseline_output.log" 2>&1
    else
      PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX="$GRADE/pycache" PYTHONPATH="$GRADE/v10shim${PYTHONPATH:+:$PYTHONPATH}" MIMO_VERIFIER_UNITTEST_JUNIT="$GRADE/baseline/unittest-junit.xml" MIMO_VERIFIER_INTEGRITY_DIR="$GRADE/baseline" MIMO_VERIFIER_JUNIT="$GRADE/baseline/junit.xml" MIMO_VERIFIER_ARCHIVE_DIR="$GRADE/baseline/junit-phases" PYTHONUNBUFFERED=1 PYTEST_ADDOPTS="--junitxml=$JUNIT_B -p no:cacheprovider" timeout 1800 sh -c "$(cat /tests/test_command.sh)" > "$V/baseline_output.log" 2>&1
    fi
    echo "$?" > "$V/baseline_rc.txt"
    echo "baseline_sec=$(($(date +%s) - START_B)) runner=$RUNNER_B rc=$(cat "$V/baseline_rc.txt")" > "$V/timing.log"
    cp "$V/baseline_output.log" "$GRADE/baseline/output.log"
  else
    echo "no baseline consumer for runner $RUNNER_B" >> "$V/baseline.log"
  fi
else
  echo "baseline hidden-test apply failed; grading keeps @5 rules" > "$V/baseline.log"
fi
# Reset to pristine bytes exactly (always, even without a baseline run).
git read-tree "$PRISTINE" || { echo "baseline reset failed" >&2; exit 1; }
git checkout-index -f -a || { echo "baseline restore failed" >&2; exit 1; }
git clean -fdq || { echo "baseline clean failed" >&2; exit 1; }
git ls-files --others --ignored --exclude-standard -z > /tmp/post_ignored.bin 2>/dev/null || : > /tmp/post_ignored.bin
if [ -s /tmp/post_ignored.bin ] || [ -s "$V/pre_ignored.bin" ]; then
  comm -z -13 "$V/pre_ignored.bin" /tmp/post_ignored.bin 2>/dev/null | xargs -0 -r rm -rf -- 2>/dev/null || true
fi
unset GIT_INDEX_FILE
# 1. The agent's change, computed by the verifier's own git against the clean
# base (same as @2).
test -f "$SNAP/workspace.tgz" || { echo "snapshot missing: $SNAP/workspace.tgz" >&2; exit 1; }
rm -rf /tmp/agentcopy && mkdir -p /tmp/agentcopy
tar -xzf "$SNAP/workspace.tgz" -C /tmp/agentcopy || { echo "snapshot unreadable" >&2; exit 1; }
rm -rf /tmp/agentcopy/.git
git --git-dir="$CWD/.git" show "$PRISTINE:.gitignore" > /tmp/base.gitignore 2>/dev/null || : > /tmp/base.gitignore
if git --git-dir="$CWD/.git" cat-file -e "$PRISTINE:.gitignore" 2>/dev/null; then
  cp /tmp/base.gitignore /tmp/agentcopy/.gitignore
else rm -f /tmp/agentcopy/.gitignore; fi
export GIT_INDEX_FILE=/tmp/agent.index; rm -f "$GIT_INDEX_FILE"
git --git-dir="$CWD/.git" --work-tree=/tmp/agentcopy read-tree "$PRISTINE" || { echo "clean-tree read failed" >&2; exit 1; }
git --git-dir="$CWD/.git" --work-tree=/tmp/agentcopy add -A
git --git-dir="$CWD/.git" --work-tree=/tmp/agentcopy diff --cached --binary "$PRISTINE" > "$V/agent.full.diff"
CHANGED=$(git --git-dir="$CWD/.git" --work-tree=/tmp/agentcopy diff --cached --name-only "$PRISTINE")
unset GIT_INDEX_FILE
# 2. @3 drops: @2 infra names and hidden-test paths, plus new-file runner
# configs and build outputs; tracked runner-config hunks revert to BASE.
TESTFILES=$(grep '^diff --git' /tests/test.patch | sed 's#.* b/##')
KEEP=/tmp/keep.list; REVERT=/tmp/revert.list; : > "$KEEP"; : > "$REVERT"; : > "$V/dropped.log"
for f in $CHANGED; do
  b=${f##*/}; drop=""; reverted=""
  case "$b" in conftest.py|pytest.ini|tox.ini|sitecustomize.py|usercustomize.py|*.pth|*_test.go) drop="test-infra";; esac
  if [ -z "$drop" ]; then
    case "$f" in */__pycache__/*|__pycache__/*) drop="binary artifact";; esac
    case "$b" in *.pyc|*.pyo|*.so|*.pyd|*.dll|*.dylib|*.o|*.a|*.class) drop="binary artifact";; esac
  fi
  if [ -z "$drop" ]; then
    case "$b" in @@V3_NEW_INFRA_CASE@@)
      if ! git --git-dir="$CWD/.git" cat-file -e "$PRISTINE:$f" 2>/dev/null; then drop="new runner config"; fi;;
    esac
  fi
  if [ -z "$drop" ]; then
    case "$f" in target/surefire-reports/*|target/failsafe-reports/*|build/test-results/*|.nyc_output/*|coverage/*)
      if ! git --git-dir="$CWD/.git" cat-file -e "$PRISTINE:$f" 2>/dev/null; then drop="new build output"; fi;;
    esac
  fi
  if [ -z "$drop" ] && printf '%s\n' "$TESTFILES" | grep -qxF -- "$f"; then drop="hidden-test path"; fi
  if [ -z "$drop" ]; then
    case "$b" in setup.cfg|pyproject.toml)
      if diff <(git --git-dir="$CWD/.git" show "$PRISTINE:$f" 2>/dev/null) "/tmp/agentcopy/$f" 2>/dev/null | grep -E '^[<>]' | grep -qiE 'pytest|addopts|plugins'; then drop="pytest config"; fi;;
    esac
  fi
  if [ -z "$drop" ]; then
    case "$f" in *.go)
      if grep -qE 'func TestMain[[:space:]]*\(' "/tmp/agentcopy/$f" 2>/dev/null; then drop="go TestMain"; fi;;
    esac
  fi
  if [ -z "$drop" ] && git --git-dir="$CWD/.git" cat-file -e "$PRISTINE:$f" 2>/dev/null; then
    CHLINES=$(diff <(git --git-dir="$CWD/.git" show "$PRISTINE:$f" 2>/dev/null) "/tmp/agentcopy/$f" 2>/dev/null | grep -E '^[<>]' || true)
    rule=""
    case "$b" in package.json)
      if printf '%s\n' "$CHLINES" | grep -qiE '"(jest|mocha|vitest|ava|tap|karma|jasmine|nyc|c8)"[[:space:]]*:'; then rule="package.json runner key"; fi;;
    esac
    case "$b" in pom.xml)
      if printf '%s\n' "$CHLINES" | grep -qiE 'surefire|failsafe|skipTests|<excludes?>'; then rule="pom.xml surefire"; fi;;
    esac
    case "$b" in build.gradle|build.gradle.kts)
      if printf '%s\n' "$CHLINES" | grep -qiE 'exclude|ignoreFailures|useJUnit|test[[:space:]]*\{'; then rule="gradle test block"; fi;;
    esac
    case "$b" in .rspec)
      if printf '%s\n' "$CHLINES" | grep -qE -- '--require|--format|--tag|--exclude-pattern'; then rule="rspec options"; fi;;
    esac
    case "$b" in phpunit.xml|phpunit.xml.dist|phpunit.dist.xml)
      if printf '%s\n' "$CHLINES" | grep -qiE 'bootstrap|suffix=|exclude|executionOrder|printer'; then rule="phpunit config"; fi;;
    esac
    case "$b" in go.mod)
      if printf '%s\n' "$CHLINES" | grep -qiE 'toolchain'; then rule="go toolchain"; fi;;
    esac
    case "$b" in .mocharc.js|.mocharc.cjs|.mocharc.mjs|.mocharc.json|.mocharc.yml|.mocharc.yaml)
      if printf '%s\n' "$CHLINES" | grep -qiE 'spec|require|ignore|exclude|grep|invert|reporter'; then rule="mocha options"; fi;;
    esac
    if [ -n "$rule" ]; then reverted="$rule"; echo "reverted $f ($rule)" >> "$V/dropped.log"; echo "$f" >> "$REVERT"; fi
  fi
  if [ -n "$drop" ]; then echo "dropped $f ($drop)" >> "$V/dropped.log"; elif [ -z "$reverted" ]; then echo "$f" >> "$KEEP"; fi
done
# 3. Copy the kept files over the pristine tree (deletions too), then
# restore reverted configs to BASE.
while IFS= read -r f; do
  [ -z "$f" ] && continue
  if [ -e "/tmp/agentcopy/$f" ] || [ -L "/tmp/agentcopy/$f" ]; then
    mkdir -p "$(dirname "$f")"
    cp -a "/tmp/agentcopy/$f" "$f"
  else rm -f "$f"; fi
done < "$KEEP"
while IFS= read -r f; do
  [ -z "$f" ] && continue
  if git --git-dir="$CWD/.git" cat-file -e "$PRISTINE:$f" 2>/dev/null; then
    git --git-dir="$CWD/.git" show "$PRISTINE:$f" > "$f"
  else rm -f "$f"; fi
done < "$REVERT"
