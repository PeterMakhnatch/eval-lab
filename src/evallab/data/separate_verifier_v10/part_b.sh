# 3b. Source that reaches into the test runner or forces the exit code is not a fix.
# Per-file gate: universal signatures on any path, exit(0) only on Ruby/PHP.
export GIT_INDEX_FILE=/tmp/kept.index; rm -f "$GIT_INDEX_FILE"
git read-tree "$PRISTINE" && git add -A . && git diff --cached "$PRISTINE" > "$V/agent.kept.diff" || {
  echo "kept-tree diff failed" >&2; exit 1
}
unset GIT_INDEX_FILE
python3 - "$V/agent.kept.diff" <<'PYEOF' > "$V/tamper.log" 2>&1
import sys, re
diff = open(sys.argv[1], encoding="utf-8", errors="replace").read()
universal = @@V3_UNIVERSAL@@
scoped = @@V3_SCOPED@@
cur = None
added = {}
for line in diff.splitlines():
    if line.startswith("+++ b/"):
        cur = line[6:]
        added.setdefault(cur, [])
    elif cur is not None and line.startswith("+") and not line.startswith("+++"):
        added[cur].append(line[1:])
for path, lines in added.items():
    text = "\n".join(lines)
    for sig in universal:
        if re.search(sig, text):
            print("tamper %s in %s" % (sig, path))
            sys.exit(10)
    leaf = path.rsplit("/", 1)[-1]
    ext = "." + leaf.rsplit(".", 1)[-1].lower() if "." in leaf else ""
    for exts, sig in scoped:
        if ext in exts and re.search(sig, text):
            print("scoped tamper %s in %s" % (sig, path))
            sys.exit(10)
PYEOF
if [ $? -eq 10 ]; then
  echo 0 > "$V/reward.txt"; echo "REWARD=0 tamper signature in agent diff"; exit 0
fi
# 4. Hidden tests, then resolve the real test command. Opaque build-env
# wrappers (mimo_build_env.tar.gz.b64, from the hidden patch) decode here:
# the tarball is trusted verifier-side bundle content, never agent bytes.
printf '%s\n' "$TESTFILES" | while IFS= read -r tf; do
  [ -z "$tf" ] && continue
  if git cat-file -e "$PRISTINE:$tf" 2>/dev/null; then
    git checkout -q "$PRISTINE" -- "$tf" 2>/dev/null || true
  else
    git rm -f --cached "$tf" >/dev/null 2>&1 || true
    rm -f "$tf"
  fi
done
if ! git apply --verbose /tests/test.patch > "$V/apply.log" 2>&1; then
  cat "$V/apply.log" >&2
  echo "the hidden tests could not be applied (testbed problem, not scored)" >&2
  exit 1
fi
RESOLVED=$(cat /tests/test_command.sh)
for ref in $(grep -oE '[^ "]*mimo_test_command\.sh' /tests/test_command.sh | sort -u); do
  base=${ref##*/}
  if [ -f "$base" ]; then RESOLVED="$RESOLVED
$(cat "$base")"; fi
done
if printf '%s' "$RESOLVED" | grep -q 'build_env/test_command'; then
  if [ -f mimo_build_env.tar.gz.b64 ]; then
    rm -rf "$GRADE/build_env" && mkdir -p "$GRADE/build_env"
    if base64 -d mimo_build_env.tar.gz.b64 2>/dev/null | tar -xzf - -C "$GRADE/build_env" 2>/dev/null; then
      BE_TC=$(find "$GRADE/build_env" -name 'test_command.sh' | head -1)
      if [ -n "$BE_TC" ]; then RESOLVED="$RESOLVED
$(cat "$BE_TC")"; fi
    fi
  fi
fi
printf '%s' "$RESOLVED" > "$V/resolved_command.txt"
RUNNER=custom
if printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])go +test( |$)'; then RUNNER=go-test
elif printf '%s' "$RESOLVED" | grep -qE 'GO_BIN" +test( |$)'; then RUNNER=go-test
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])jest([^[:alnum:]_]|$)'; then RUNNER=jest
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])vitest([^[:alnum:]_]|$)'; then RUNNER=vitest
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])_?mocha([^[:alnum:]_]|$)'; then RUNNER=mocha
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])ava([^[:alnum:]_]|$)'; then RUNNER=ava
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])tap([^[:alnum:]_]|$)'; then RUNNER=tap
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])karma([^[:alnum:]_]|$)'; then RUNNER=karma
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])jasmine([^[:alnum:]_]|$)'; then RUNNER=jasmine
elif printf '%s' "$RESOLVED" | grep -qE 'node +--test([^[:alnum:]_]|$)'; then RUNNER=node-test
elif printf '%s' "$RESOLVED" | grep -qE 'cargo +test([^[:alnum:]_]|$)'; then RUNNER=cargo-test
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])rspec([^[:alnum:]_]|$)'; then RUNNER=rspec
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])phpunit([^[:alnum:]_]|$)'; then RUNNER=phpunit
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])mvn([^[:alnum:]_]|$)|surefire|failsafe'; then RUNNER=mvn
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])gradlew?([^[:alnum:]_]|$)'; then RUNNER=gradle
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])pytest([^[:alnum:]_]|$)'; then RUNNER=pytest
elif printf '%s' "$RESOLVED" | grep -qE '\-m +unittest([^[:alnum:]_]|$)'; then RUNNER=unittest
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])forge +test([^[:alnum:]_]|$)'; then RUNNER=forge
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])node([^[:alnum:]_]|$)'; then RUNNER=node-run
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])(make|ctest|cmake)([^[:alnum:]_]|$)'; then RUNNER=make
elif printf '%s' "$RESOLVED" | grep -qE '(^|[^[:alnum:]_])bats([^[:alnum:]_]|$)'; then RUNNER=bats
fi
echo "RUNNER=$RUNNER" > "$V/runner.txt"
# Verifier-owned conftest hook for pytest commands that clear our addopts:
# a root conftest.py is always collected, so the structured report survives
# any env scrubbing. Appended after the hidden-test apply, never clobbering.
if [ "$RUNNER" = pytest ]; then
  CLEARED=0
  printf '%s' "$RESOLVED" | grep -q 'unset.*PYTEST_ADDOPTS' && CLEARED=1
  printf '%s' "$RESOLVED" | grep -q 'PYTEST_ADDOPTS=' && CLEARED=1
  printf '%s' "$RESOLVED" | grep -qE 'env +([^ ]+ +)*-u([^ ]* +)*PYTEST_ADDOPTS|env +-u +PYTEST_ADDOPTS' && CLEARED=1
  if [ "$CLEARED" = 1 ]; then
    echo "@@V3_CONFTEST_HOOK_B64@@" | base64 -d >> ./conftest.py
    touch "$GRADE/hook_installed"
    echo "installed verifier conftest hook (addopts cleared)" >> "$V/dropped.log"
  fi
  python3 - "$RESOLVED" "$CWD" > "$V/conftest-targets.txt" 2>/dev/null <<'PYEOF' || true
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
PYEOF
  while IFS= read -r _v5_d; do
    [ -n "$_v5_d" ] || continue
    _v5_cf="$CWD/$_v5_d/conftest.py"
    mkdir -p "$(dirname "$_v5_cf")"
    if ! grep -q "mimo-junit-archive/1" "$_v5_cf" 2>/dev/null; then
      echo "@@V5_ARCHIVE_HOOK_B64@@" | base64 -d >> "$_v5_cf"
    fi
  done < "$V/conftest-targets.txt"
  echo "installed verifier junit archive hook" >> "$V/dropped.log"
  while IFS= read -r _v10_d; do
    [ -n "$_v10_d" ] || continue
    _v10_cf="$CWD/$_v10_d/conftest.py"
    mkdir -p "$(dirname "$_v10_cf")"
    if ! grep -q "mimo-integrity/1" "$_v10_cf" 2>/dev/null; then
      echo "@@V10_INTEGRITY_HOOK_B64@@" | base64 -d >> "$_v10_cf"
    fi
  done < "$V/conftest-targets.txt"
  echo "installed verifier integrity hook" >> "$V/dropped.log"
fi
export MIMO_VERIFIER_JUNIT="$GRADE/junit.xml"
