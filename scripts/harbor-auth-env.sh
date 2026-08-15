# shellcheck shell=bash
#
# Source this file to give the current shell the credentials Harbor needs for
# Claude-subscription and Codex-subscription runs.
#
#     source scripts/harbor-auth-env.sh
#
# This file contains no secret. The Claude OAuth token lives only in the macOS
# login Keychain and is read into the process environment at runtime. Nothing
# here prints, logs, or writes the token.
#
# Store the token first with:  scripts/claude-token-setup.sh
# Check status any time with:  scripts/auth-status.sh

HARBOR_CLAUDE_KEYCHAIN_SERVICE="${HARBOR_CLAUDE_KEYCHAIN_SERVICE:-harbor-practice-claude-oauth}"
HARBOR_CLAUDE_KEYCHAIN_ACCOUNT="${HARBOR_CLAUDE_KEYCHAIN_ACCOUNT:-$USER}"

# Read the token into the environment. `security` writes it to stdout, so the
# value is captured directly into the variable and never echoed. The `|| true`
# keeps a missing item from killing a caller running under `set -e`; callers
# decide what to do about an empty token themselves.
_harbor_auth_token="$(
    /usr/bin/security find-generic-password \
        -s "$HARBOR_CLAUDE_KEYCHAIN_SERVICE" \
        -a "$HARBOR_CLAUDE_KEYCHAIN_ACCOUNT" \
        -w 2>/dev/null || true
)"

if [ -z "$_harbor_auth_token" ]; then
    echo "harbor-auth-env: no Claude OAuth token in Keychain" >&2
    echo "harbor-auth-env: run scripts/claude-token-setup.sh first" >&2
    unset _harbor_auth_token
else
    # Claude Code CLI (Harbor's claude-code agent) subscription auth.
    export CLAUDE_CODE_OAUTH_TOKEN="$_harbor_auth_token"

    unset _harbor_auth_token
fi

# Make the Claude Code CLI drop ANTHROPIC_API_KEY and use the subscription.
export CLAUDE_FORCE_OAUTH=1

# Make Reward Kit's Anthropic judge prefer CLAUDE_CODE_OAUTH_TOKEN over an
# ambient ANTHROPIC_API_KEY.
export REWARDKIT_FORCE_OAUTH=1

# Make Harbor's codex agent inject ~/.codex/auth.json (ChatGPT subscription)
# instead of expecting OPENAI_API_KEY.
export CODEX_FORCE_AUTH_JSON=1
