#!/bin/bash
# Reference solution: incremental recalculation with a reverse-dependency index,
# iterative Tarjan SCC for cycles, and Kahn ordering so each cell is evaluated once.
set -euo pipefail
cp "$(dirname "$0")/engine.py" /app/sheet/engine.py
