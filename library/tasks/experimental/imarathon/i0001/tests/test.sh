#!/bin/sh
set -u
mkdir -p /logs/verifier
python3 /tests/verify_imarathon.py
