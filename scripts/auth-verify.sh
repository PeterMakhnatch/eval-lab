#!/bin/bash
# Prove the stored Claude subscription token actually authenticates, by making
# one minimal request to the Anthropic Messages API using exactly the headers
# LiteLLM builds for an `sk-ant-oat*` token. This is the same credential path
# Reward Kit's Anthropic LLM judge and Harbor's Generate Analysis use.
#
# Costs one request of a handful of tokens against the subscription.
# Prints the HTTP status and nothing from the credential.
set -uo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=./harbor-auth-env.sh
source "$script_dir/harbor-auth-env.sh"

if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    echo "auth-verify: no token available; run scripts/claude-token-setup.sh" >&2
    exit 78
fi

MODEL="${HARBOR_AUTH_VERIFY_MODEL:-claude-sonnet-4-6}"

# The token is read from the environment inside Python, so it never appears in
# argv. Only the HTTP status and any error type are printed.
CLAUDE_VERIFY_MODEL="$MODEL" python3 - <<'PY'
import json
import os
import sys
import urllib.error
import urllib.request

token = os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
model = os.environ["CLAUDE_VERIFY_MODEL"]

# Header set copied from litellm.llms.anthropic.common_utils for OAuth tokens.
headers = {
    "authorization": f"Bearer {token}",
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "anthropic-dangerous-direct-browser-access": "true",
    "content-type": "application/json",
    "accept": "application/json",
}
body = json.dumps(
    {
        "model": model,
        "max_tokens": 4,
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
    }
).encode()

request = urllib.request.Request(
    "https://api.anthropic.com/v1/messages", data=body, headers=headers
)

try:
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read())
    print(f"HTTP status:            {response.status}")
    print(f"model reported:         {payload.get('model', '?')}")
    print(f"response text present:  {'yes' if payload.get('content') else 'no'}")
    print("subscription auth:      WORKING")
except urllib.error.HTTPError as error:
    detail = error.read().decode(errors="replace")[:400]
    print(f"HTTP status:            {error.code}")
    print(f"subscription auth:      FAILED")
    print(f"error body (truncated): {detail}")
    sys.exit(1)
except Exception as error:  # noqa: BLE001 - surface any transport problem plainly
    print(f"subscription auth:      FAILED ({type(error).__name__}: {error})")
    sys.exit(1)
PY
