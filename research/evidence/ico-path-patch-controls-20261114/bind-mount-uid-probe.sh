#!/bin/bash
# Retained probe: does chmod 700 + root ownership on a bind-mounted directory
# stop a uid-dropped (nobody) child from writing there on this Docker Desktop
# file-sharing backend? Reproduces the permission model of /logs/verifier.
set -u
OUT=/private/tmp/har-434-controls-RctCnV/bind-mount-uid-probe
LOG="$OUT/probe.log"
mkdir -p "$OUT/verifier"
: > "$LOG"

{
  echo "== started_at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "== host: $(uname -srm)"
  echo "== docker client: $(docker version --format '{{.Client.Version}}' 2>&1)"
  echo "== docker server: $(docker version --format '{{.Server.Version}} {{.Server.Arch}} {{.Server.Os}}' 2>&1)"
  echo "== probe dir: $OUT/verifier"
} >> "$LOG"

CMD=(docker run --rm --network none -v "$OUT/verifier":/logs/verifier python:3.12-slim sh -c 'mkdir -p /logs/verifier; chmod 700 /logs/verifier; echo "inside: $(ls -ld /logs/verifier)"; id nobody; setpriv --reuid=nobody --regid=nogroup --clear-groups sh -c "echo forged > /logs/verifier/reward.txt"; echo "nobody_write_rc=$?"; ls -l /logs/verifier; echo 0 > /logs/verifier/reward.txt; echo "root_write_rc=$?"')

printf '== command: ' >> "$LOG"
printf '%q ' "${CMD[@]}" >> "$LOG"
printf '\n' >> "$LOG"
"${CMD[@]}" >> "$LOG" 2>&1
RC=$?
echo "== exit_status $RC" >> "$LOG"
{
  echo "== host_side_stat:"
  stat -f '  %N mode=%Sp uid=%u gid=%g size=%z' "$OUT/verifier" "$OUT/verifier/reward.txt" 2>&1
  echo "== host_side_reward_content: $(cat "$OUT/verifier/reward.txt" 2>/dev/null)"
  echo "== finished_at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >> "$LOG"
cat "$LOG"
exit "$RC"
