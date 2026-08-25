#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from materializer import materialize, output_path, reject_committed_corpora

parser = argparse.ArgumentParser(description="Materialize the ignored LOCA lean canary")
parser.add_argument("--output", type=Path, default=None)
parser.add_argument("--cache-dir", type=Path, default=None)
parser.add_argument("--verify-sources", action="store_true")
args = parser.parse_args()
reject_committed_corpora()
print(json.dumps(materialize(args.output, cache_dir=args.cache_dir, verify_sources=args.verify_sources), sort_keys=True))
