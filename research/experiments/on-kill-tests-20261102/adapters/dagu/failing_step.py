#!/usr/bin/env python3
import json
import os
import sys
import time

ledger_path = sys.argv[1]
with open(ledger_path, "a", encoding="utf-8") as f:
    entry = {"id": "FAIL_TASK", "pid": os.getpid(), "t": time.time(), "kind": "exec"}
    f.write(json.dumps(entry) + "\n")

sys.exit(1)
