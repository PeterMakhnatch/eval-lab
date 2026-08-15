#!/bin/sh
set -eu
tr '[:lower:]' '[:upper:]' < /app/input.txt > /app/output/result.txt
