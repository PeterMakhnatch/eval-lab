#!/bin/bash
set -u

exec python3 - @POLICY@ @MODE@ <<'PYTHON'
@AGENT@
PYTHON
