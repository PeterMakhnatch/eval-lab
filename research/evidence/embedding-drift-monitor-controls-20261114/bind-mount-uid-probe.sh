#!/bin/bash
# Disposable filesystem probe only. Never mount a real task or control directory.
# Preserve the scratch directory and its raw log after the container exits.
set -u
OUT=$(mktemp -d /private/tmp/har-438-bind-mount-uid-XXXXXX) || exit 1
OUT=$(cd "$OUT" && pwd -P) || exit 1
LOG="$OUT/probe.log"
mkdir "$OUT/verifier" || exit 1

{
  echo "== started_at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "== probe_kind disposable Docker filesystem probe; not a task/control trial"
  echo "== host: $(uname -srm)"
  echo "== scratch_path: $OUT"
  echo "== scratch_log: $LOG"
  echo "== verifier_path: $OUT/verifier"
  echo "== docker client/server:"
  docker version --format 'client_version={{.Client.Version}} client_arch={{.Client.Arch}} client_os={{.Client.Os}} server_version={{.Server.Version}} server_arch={{.Server.Arch}} server_os={{.Server.Os}}'
  echo "docker_version_rc=$?"
  echo "== locally present image:"
  docker image inspect python:3.12-slim --format 'id={{.Id}} os={{.Os}} architecture={{.Architecture}}'
  IMAGE_RC=$?
  echo "docker_image_inspect_rc=$IMAGE_RC"
  echo "== host_side_stat_before:"
  stat -f '  %N mode=%Sp uid=%u gid=%g size=%z' "$OUT" "$OUT/verifier"
  echo "host_stat_before_rc=$?"
} > "$LOG" 2>&1

if [ "$IMAGE_RC" -ne 0 ]; then
  {
    echo "== refused: python:3.12-slim must already exist; no pull permitted"
    echo "== exit_status $IMAGE_RC"
    echo "== finished_at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >> "$LOG"
  cat "$LOG"
  exit "$IMAGE_RC"
fi

CMD=(docker run --rm --pull never --network none \
  --mount "type=bind,src=$OUT/verifier,dst=/logs/verifier" \
  python:3.12-slim sh -c '
    echo "== container root identity:"
    id
    chown 0:0 /logs/verifier
    echo "root_chown_rc=$?"
    chmod 700 /logs/verifier
    echo "root_chmod_rc=$?"
    echo "== container directory after chown/chmod:"
    stat -c "%n mode=%A numeric_mode=%a uid=%u gid=%g" /logs/verifier
    echo "container_directory_stat_rc=$?"
    echo "== nobody identity and reward write attempt:"
    setpriv --reuid=nobody --regid=nogroup --clear-groups sh -c '\''id; printf "forged\n" > /logs/verifier/reward.txt'\''
    echo "nobody_write_rc=$?"
    echo "== reward content after nobody attempt (read by root):"
    cat /logs/verifier/reward.txt
    echo "nobody_result_read_rc=$?"
    echo "== root reward write:"
    printf "0\n" > /logs/verifier/reward.txt
    echo "root_write_rc=$?"
    stat -c "%n mode=%A numeric_mode=%a uid=%u gid=%g" /logs/verifier /logs/verifier/reward.txt
    echo "container_final_stat_rc=$?"
    echo "== root reward content:"
    cat /logs/verifier/reward.txt
    echo "root_result_read_rc=$?"
  ')

printf '== command: ' >> "$LOG"
printf '%q ' "${CMD[@]}" >> "$LOG"
printf '\n' >> "$LOG"
"${CMD[@]}" >> "$LOG" 2>&1
RC=$?
{
  echo "== exit_status $RC"
  echo "== host_side_stat_after:"
  stat -f '  %N mode=%Sp uid=%u gid=%g size=%z' "$OUT" "$OUT/verifier" "$OUT/verifier/reward.txt"
  echo "host_stat_after_rc=$?"
  echo "== host_side_reward_content:"
  cat "$OUT/verifier/reward.txt"
  echo "host_reward_read_rc=$?"
  echo "== retained_scratch_path: $OUT"
  echo "== finished_at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >> "$LOG" 2>&1
cat "$LOG"
exit "$RC"
