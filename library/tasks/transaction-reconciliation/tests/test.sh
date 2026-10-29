#!/bin/sh
set -eu

# Grading runs in the separate verifier image built from tests/Dockerfile.
# Nothing is installed or fetched here, and the interpreter is called by
# absolute path so nothing on PATH can stand in for it.
exec /usr/local/bin/python3 -I /tests/verify.py
