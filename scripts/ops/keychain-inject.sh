#!/bin/bash
# Never prints EVAL_LAB_SECRET_REF or secret values. Logs only probe= and present=.
set +x
set -uo pipefail

python3 - <<'PY'
import os
import re
import subprocess
import sys

GRAMMAR = re.compile(r"^keychain:[A-Za-z0-9._-]{1,64}/[A-Za-z0-9._-]{1,64}$")
ref = os.environ.get("EVAL_LAB_SECRET_REF", "")
present = os.environ.get("EVAL_LAB_SECRET_PRESENT", "") in {"1", "true"}
if ref and GRAMMAR.fullmatch(ref) is None:
    print("probe=absent")
    print("present=no")
    print("reason=missing_secret")
    raise SystemExit(2)
if present:
    print("probe=injected")
    print("present=yes")
    raise SystemExit(0)
if sys.platform == "darwin" and os.access("/usr/bin/security", os.X_OK):
    service = os.environ.get("HARBOR_CLAUDE_KEYCHAIN_SERVICE", "harbor-practice-claude-oauth")
    account = os.environ.get("HARBOR_CLAUDE_KEYCHAIN_ACCOUNT") or os.environ.get("USER", "")
    label = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
    if label.fullmatch(service) and label.fullmatch(account):
        completed = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", service, "-a", account],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if completed.returncode == 0:
            print("probe=keychain-existence-only")
            print("present=yes")
            raise SystemExit(0)
print("probe=absent")
print("present=no")
print("reason=missing_secret")
raise SystemExit(2)
PY
