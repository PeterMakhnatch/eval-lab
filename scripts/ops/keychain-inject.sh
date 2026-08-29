#!/bin/bash
# Prove a keychain or env *reference* exists. Never prints a secret value.
set -uo pipefail

service="${HARBOR_CLAUDE_KEYCHAIN_SERVICE:-harbor-practice-claude-oauth}"
account="${HARBOR_CLAUDE_KEYCHAIN_ACCOUNT:-$USER}"
ref="${EVAL_LAB_SECRET_REF:-keychain:${service}/${account}}"

echo "secret_ref=$ref"

if [ "${EVAL_LAB_SECRET_PRESENT:-}" = "1" ] || [ "${EVAL_LAB_SECRET_PRESENT:-}" = "true" ]; then
    echo "present=yes"
    echo "probe=injected"
    exit 0
fi

if [ -n "${EVAL_LAB_SECRET_REF:-}" ] && [ -z "${EVAL_LAB_SECRET_PRESENT:-}" ]; then
    echo "present=no"
    echo "reason=missing_secret"
    exit 2
fi

if [ "$(uname -s)" = "Darwin" ] && [ -x /usr/bin/security ]; then
    if /usr/bin/security find-generic-password -s "$service" -a "$account" >/dev/null 2>&1; then
        echo "present=yes"
        echo "probe=keychain-existence-only"
        exit 0
    fi
fi

echo "present=no"
echo "reason=missing_secret"
exit 2
