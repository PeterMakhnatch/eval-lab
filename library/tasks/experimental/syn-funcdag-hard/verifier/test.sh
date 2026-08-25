#!/bin/sh
set -eu
mkdir -p /logs/verifier
exec python /tests/verify.py
