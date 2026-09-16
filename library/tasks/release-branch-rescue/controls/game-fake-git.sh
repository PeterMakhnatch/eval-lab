#!/bin/bash
# expect: 0
# Anti-gaming: fabricate successful output by replacing git binary in the agent container.
# In separate verifier mode, the verifier runs its own trusted git, so this fails.
set -euo pipefail

cat << 'EOF' > /usr/local/bin/git
#!/bin/sh
echo "All git operations succeeded"
exit 0
EOF
chmod +x /usr/local/bin/git
