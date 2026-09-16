#!/bin/bash
set -euo pipefail

export GIT_AUTHOR_NAME="Release Bot"
export GIT_AUTHOR_EMAIL="release@example.com"
export GIT_COMMITTER_NAME="Release Bot"
export GIT_COMMITTER_EMAIL="release@example.com"
export GIT_CONFIG_GLOBAL=/etc/gitconfig
export GIT_CONFIG_SYSTEM=/dev/null

# Persist global Git identity across shells
git config --file /etc/gitconfig user.name "Release Bot"
git config --file /etc/gitconfig user.email "release@example.com"

rm -rf /srv/origin.git /workspace/release /tmp/upstream-dev
mkdir -p /srv /workspace /tmp/upstream-dev

# Initialize bare upstream
git init --bare /srv/origin.git

# Initialize temporary upstream workspace for baseline A
cd /tmp/upstream-dev
git init -b main
mkdir -p config docs

cat << 'EOF' > config/limits.conf
# Service rate limits and retries
max_connections=100
retries=1
EOF

cat << 'EOF' > config/timeouts.conf
# Client and upstream timeout settings
connect_timeout=5
timeout=30
EOF

cat << 'EOF' > config/regions.txt
us-east
EOF

cat << 'EOF' > docs/runbook.md
# Incident Response and Deployment Runbook

## Deployment Verification
1. Verify gateway connectivity.
2. Confirm health check responses on all endpoints.
EOF

git add config docs
export GIT_AUTHOR_DATE="2026-01-01T10:00:00Z"
export GIT_COMMITTER_DATE="2026-01-01T10:00:00Z"
git commit -m "release: v1.0.0 initial baseline"
git tag -a v1 -m "v1.0.0 release"

git remote add origin /srv/origin.git
git push origin main
git push origin v1

# Clone origin to /workspace/release while at commit A
git clone --branch main /srv/origin.git /workspace/release
cd /workspace/release

# Configure local repository identity
git config user.name "Release Bot"
git config user.email "release@example.com"

# Simulate recovery attempt: unconfigured tracking, disabled GC, preserved reflog
git branch --unset-upstream main
git config gc.auto 0
git config gc.pruneExpire never
git config gc.reflogExpire never
git config gc.reflogExpireUnreachable never

# Create local unpublished commits L1 and L2
cat << 'EOF' > config/limits.conf
# Service rate limits and retries
max_connections=100
retries=3
EOF
git add config/limits.conf
export GIT_AUTHOR_DATE="2026-01-02T14:00:00Z"
export GIT_COMMITTER_DATE="2026-01-02T14:00:00Z"
git commit -m "release: increase retry count to 3"

cat << 'EOF' > config/regions.txt
us-east
eu-west
EOF
git add config/regions.txt
export GIT_AUTHOR_DATE="2026-01-03T14:00:00Z"
export GIT_COMMITTER_DATE="2026-01-03T14:00:00Z"
git commit -m "release: add eu-west deployment region"

# Reset local main to v1 and detach HEAD at v1
git reset --hard v1
git checkout --detach v1

# Advance upstream with R1 and R2 in upstream-dev
cd /tmp/upstream-dev
cat << 'EOF' > config/timeouts.conf
# Client and upstream timeout settings
connect_timeout=5
timeout=60
EOF
git add config/timeouts.conf
export GIT_AUTHOR_DATE="2026-01-02T10:00:00Z"
export GIT_COMMITTER_DATE="2026-01-02T10:00:00Z"
git commit -m "upstream: increase timeout to 60s for slow backends"

cat << 'EOF' > docs/runbook.md
# Incident Response and Deployment Runbook

## Deployment Verification
1. Verify gateway connectivity.
2. Confirm health check responses on all endpoints.
3. Check upstream latency metrics in telemetry dashboard.
EOF
git add docs/runbook.md
export GIT_AUTHOR_DATE="2026-01-03T10:00:00Z"
export GIT_COMMITTER_DATE="2026-01-03T10:00:00Z"
git commit -m "upstream: document latency metrics check in runbook"

git push origin main

# Cleanup temporary upstream build directory
cd /
rm -rf /tmp/upstream-dev
