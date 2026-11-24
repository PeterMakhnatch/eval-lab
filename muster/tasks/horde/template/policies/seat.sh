#!/bin/bash
set -u
exec python3 -u - <<'PY'
ROOMS = @ROOMS@
SIZE = @SIZE@
TREASURE = @TREASURE@
RULE = "@RULE@"
POLICY = "@POLICY@"
PLAN = @PLAN@
@CODE@
PY
