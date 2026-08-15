#!/bin/sh
set -eu
mkdir -p /app/output
printf 'ALPHA-BETA-GAMMA\nUNEXPECTED\n' > /app/output/result.txt
printf 'leak\n' > /app/output/extra.txt
