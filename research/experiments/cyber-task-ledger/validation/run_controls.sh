#!/bin/bash
# Cyber variant pipeline controls: do-nothing and wrong-PoC, one container each.
#
# Usage:
#   run_controls.sh <task_id> <image-ref> <package-dir> <out-dir>
#
# Faithful to the investigator's controls (MimoCyberMusicTerminal report §1):
# proper setup.sh run ("setup done", server live on :8666), then
# python3 /tests/verify.py via tests/test.sh with no PoC (do-nothing), and a
# second container where /home/agent/.bashrc is submitted as user `agent`
# through /home/agent/submit.sh before grading (wrong-PoC). All local Docker,
# --network none, one container at a time, images already cached (0 pulls).
set -u
TASK="$1"
IMAGE="$2"
PKG="$(cd "$3" && pwd)"
OUT="$(mkdir -p "$4" && cd "$4" && pwd)"

mkdir -p "$OUT/do-nothing" "$OUT/wrong-poc"

SETUP_BODY='
set -e
mkdir -p /var/lib/mimo /tests /logs/verifier
cp -r /pkg-setup/. /var/lib/mimo/
cp -r /pkg-tests/. /tests/
bash /var/lib/mimo/setup.sh
echo "--- setup exit: $?"
'

echo "== $TASK do-nothing =="
docker run --rm --network none --platform linux/amd64 \
  -v "$PKG/environment/setup:/pkg-setup:ro" \
  -v "$PKG/tests:/pkg-tests:ro" \
  -v "$OUT/do-nothing:/out" \
  "$IMAGE" \
  bash -c "$SETUP_BODY
    bash /tests/test.sh
    echo \"--- grade exit: \$?\"
    cp /logs/verifier/result.json /logs/verifier/reward.txt /out/" \
  > "$OUT/do-nothing/console.log" 2>&1
echo "do-nothing docker exit: $?"

echo "== $TASK wrong-poc =="
docker run --rm --network none --platform linux/amd64 \
  -v "$PKG/environment/setup:/pkg-setup:ro" \
  -v "$PKG/tests:/pkg-tests:ro" \
  -v "$OUT/wrong-poc:/out" \
  "$IMAGE" \
  bash -c "$SETUP_BODY
    su -s /bin/bash agent -c '/home/agent/submit.sh /home/agent/.bashrc'
    echo \"--- submit exit: \$?\"
    bash /tests/test.sh
    echo \"--- grade exit: \$?\"
    cp /logs/verifier/result.json /logs/verifier/reward.txt /out/" \
  > "$OUT/wrong-poc/console.log" 2>&1
echo "wrong-poc docker exit: $?"
