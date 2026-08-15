#!/bin/bash
# Report whether Harbor's credentials are available. Prints booleans and shapes
# only -- never a credential value, prefix, or suffix.
set -uo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SERVICE="${HARBOR_CLAUDE_KEYCHAIN_SERVICE:-harbor-practice-claude-oauth}"
ACCOUNT="${HARBOR_CLAUDE_KEYCHAIN_ACCOUNT:-$USER}"

yesno() { if [ "$1" = "0" ]; then echo yes; else echo no; fi; }

echo "== Harbor auth status =="
echo

/usr/bin/security find-generic-password -s "$SERVICE" -a "$ACCOUNT" >/dev/null 2>&1
echo "keychain item present ($SERVICE):    $(yesno $?)"

token="$(/usr/bin/security find-generic-password -s "$SERVICE" -a "$ACCOUNT" -w 2>/dev/null)"
if [ -n "$token" ]; then
    echo "keychain item readable non-interactively: yes"
    case "$token" in
        sk-ant-oat*) echo "token shape is a Claude OAuth token:     yes" ;;
        *)           echo "token shape is a Claude OAuth token:     no (expected sk-ant-oat...)" ;;
    esac
else
    echo "keychain item readable non-interactively: no"
    echo "token shape is a Claude OAuth token:     n/a"
fi
unset token

echo
echo "-- exported by scripts/harbor-auth-env.sh --"
# Run the sourcing in a subshell so this script never mutates the caller.
(
    # shellcheck source=./harbor-auth-env.sh
    source "$script_dir/harbor-auth-env.sh" 2>/dev/null
    echo "CLAUDE_CODE_OAUTH_TOKEN set:  $([ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && echo yes || echo no)"
    echo "CLAUDE_FORCE_OAUTH:           ${CLAUDE_FORCE_OAUTH:-<unset>}"
    echo "REWARDKIT_FORCE_OAUTH:        ${REWARDKIT_FORCE_OAUTH:-<unset>}"
    echo "CODEX_FORCE_AUTH_JSON:        ${CODEX_FORCE_AUTH_JSON:-<unset>}"
)

echo
echo "-- other prerequisites --"
echo "claude CLI on PATH:           $(command -v claude >/dev/null 2>&1 && echo yes || echo no)"
echo "codex CLI on PATH:            $(command -v codex >/dev/null 2>&1 && echo yes || echo no)"
echo "codex auth.json present:      $([ -f "$HOME/.codex/auth.json" ] && echo yes || echo no)"
echo "harbor CLI on PATH:           $(command -v harbor >/dev/null 2>&1 && echo yes || echo no)"
echo "docker daemon reachable:      $(docker info >/dev/null 2>&1 && echo yes || echo no)"
