#!/bin/bash
set -u

exec python3 - @POLICY@ @MODE@ <<'PYTHON'
TAGS_JSON = r'''@TAGS@'''
@AGENT@
PYTHON
