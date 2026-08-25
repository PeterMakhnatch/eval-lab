#!/bin/sh
set -eu
awk '{ print toupper($0) }' /app/input.txt > /app/output/result.txt
