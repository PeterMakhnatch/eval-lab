#!/bin/bash
set -eu
exec python3 - @POLICY@ <<'PYTHON'
@ZOO@
@DRIVER@
main(POLICIES)
PYTHON
