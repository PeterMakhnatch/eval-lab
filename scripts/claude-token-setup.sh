#!/bin/bash
# One-time interactive setup: obtain a Claude subscription OAuth token and store
# it in the macOS login Keychain.
#
# Run this in your own terminal:
#
#     scripts/claude-token-setup.sh
#
# What happens:
#   1. `claude setup-token` opens your browser for OAuth and prints a token
#      ending up in THIS terminal only.
#   2. `security` prompts for that token with hidden input and writes it to the
#      Keychain.
#
# The token is never passed as a command-line argument, never written to a file,
# never added to shell history, and never printed by this script.
set -uo pipefail

SERVICE="${HARBOR_CLAUDE_KEYCHAIN_SERVICE:-harbor-practice-claude-oauth}"
ACCOUNT="${HARBOR_CLAUDE_KEYCHAIN_ACCOUNT:-$USER}"

if [ ! -t 0 ]; then
    echo "error: this script needs an interactive terminal." >&2
    exit 66
fi

cat <<EOF
== Claude subscription token setup ==

Keychain service: $SERVICE
Keychain account: $ACCOUNT

Step 1 of 2 - browser OAuth.
  'claude setup-token' will open your browser. After you approve, it prints a
  token starting with sk-ant-oat in this terminal. Copy it.
  Requires a Claude Pro/Max/Team/Enterprise subscription.

EOF

read -r -p "Press Return to run 'claude setup-token' (or type 'skip' if you already have the token): " answer

if [ "$answer" != "skip" ]; then
    if ! command -v claude >/dev/null 2>&1; then
        echo "error: 'claude' CLI not found on PATH." >&2
        exit 69
    fi
    claude setup-token
    status=$?
    if [ "$status" -ne 0 ]; then
        echo >&2
        echo "error: 'claude setup-token' exited $status. Nothing was stored." >&2
        exit "$status"
    fi
fi

cat <<EOF

Step 2 of 2 - store it in the Keychain.
  'security' will prompt twice with hidden input. Paste the token at both
  prompts. Nothing is echoed.

EOF

# -w as the final option makes security prompt for the value with hidden input,
# so the token never appears in argv, shell history, or any file.
# -T /usr/bin/security lets later reads succeed without a GUI approval dialog.
/usr/bin/security add-generic-password \
    -U \
    -s "$SERVICE" \
    -a "$ACCOUNT" \
    -D "Claude subscription OAuth token" \
    -j "Used by harbor-practice/scripts/harbor-auth-env.sh. Rotate with claude setup-token." \
    -T /usr/bin/security \
    -w
status=$?

if [ "$status" -ne 0 ]; then
    echo >&2
    echo "error: storing the token failed (security exited $status)." >&2
    exit "$status"
fi

echo
echo "Stored. Verifying (no value is printed):"
echo

stored="$(/usr/bin/security find-generic-password -s "$SERVICE" -a "$ACCOUNT" -w 2>/dev/null)"
if [ -z "$stored" ]; then
    echo "  readable non-interactively: no  <- unexpected, re-run this script" >&2
    exit 70
fi
echo "  readable non-interactively: yes"
case "$stored" in
    sk-ant-oat*)
        echo "  looks like a Claude OAuth token: yes"
        ;;
    *)
        echo "  looks like a Claude OAuth token: NO"
        echo
        echo "  The stored value does not start with 'sk-ant-oat'. That usually means" >&2
        echo "  something other than the token was pasted. Re-run this script." >&2
        unset stored
        exit 65
        ;;
esac
unset stored

cat <<EOF

Done. Next:

  scripts/auth-status.sh                # booleans only
  scripts/auth-verify.sh                # one tiny live API call
  scripts/with-claude-auth harbor view  # viewer with Generate Analysis auth

To rotate or revoke:

  scripts/claude-token-setup.sh                                        # replace
  security delete-generic-password -s "$SERVICE" -a "$ACCOUNT"   # remove
EOF
