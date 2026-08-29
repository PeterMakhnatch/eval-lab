#!/bin/bash
# Prove a keychain reference exists. Never prints a secret value or a caller
# EVAL_LAB_SECRET_REF. Closed grammar only; logs fixed probe type and presence.
set -uo pipefail

present_injected=0
if [ "${EVAL_LAB_SECRET_PRESENT:-}" = "1" ] || [ "${EVAL_LAB_SECRET_PRESENT:-}" = "true" ]; then
    present_injected=1
fi

ref="${EVAL_LAB_SECRET_REF:-}"
if [ -n "$ref" ]; then
    # Validate closed grammar without echoing the value.
    case "$ref" in
        keychain:[A-Za-z0-9._-][A-Za-z0-9._-]*/[A-Za-z0-9._-][A-Za-z0-9._-]*)
            ;;
        *)
            echo "probe=absent"
            echo "present=no"
            echo "reason=missing_secret"
            exit 2
            ;;
    esac
fi

if [ "$present_injected" = "1" ]; then
    echo "probe=injected"
    echo "present=yes"
    exit 0
fi

if [ "$(uname -s)" = "Darwin" ] && [ -x /usr/bin/security ]; then
    service="${HARBOR_CLAUDE_KEYCHAIN_SERVICE:-harbor-practice-claude-oauth}"
    account="${HARBOR_CLAUDE_KEYCHAIN_ACCOUNT:-$USER}"
    case "$service" in
        [A-Za-z0-9._-][A-Za-z0-9._-]*) ;;
        *)
            echo "probe=absent"
            echo "present=no"
            echo "reason=missing_secret"
            exit 2
            ;;
    esac
    case "$account" in
        [A-Za-z0-9._-][A-Za-z0-9._-]*) ;;
        *)
            echo "probe=absent"
            echo "present=no"
            echo "reason=missing_secret"
            exit 2
            ;;
    esac
    if /usr/bin/security find-generic-password -s "$service" -a "$account" >/dev/null 2>&1; then
        echo "probe=keychain-existence-only"
        echo "present=yes"
        exit 0
    fi
fi

echo "probe=absent"
echo "present=no"
echo "reason=missing_secret"
exit 2
