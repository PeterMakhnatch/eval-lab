#!/bin/bash
set -u
exec python3 - @POLICY@ <<'PYTHON'
@ZOO@
@DRIVER@
main(POLICIES)
PYTHON
