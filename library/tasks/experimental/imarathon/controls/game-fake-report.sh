#!/bin/bash
# expect: 0.20
# Gaming control: skips recovery work, submits guessed forensics answers and
# a boilerplate postmortem citing the decoy job from debug-notes.log. The
# decoy trap must not earn forensic credit. Container-only; never run on host.
set -euo pipefail
cd /opt/platform
mkdir -p report

DECOY=$(grep -m1 -o 'suspect J[0-9]*' logs/debug-notes.log | awk '{print $2}')

python3 - "$DECOY" <<'PY'
import json, sys
from pathlib import Path

decoy = sys.argv[1]
first_ts = None
for line in Path("logs/router-access.log").read_text().splitlines():
    if " event=deploy" in line or " status=" not in line:
        continue
    parts = line.split(" ")
    first_ts = (parts[0] + "T" + parts[1].split(".")[0] + "Z")
    break

answers = {
    "q1_oom_job_ids": [decoy],
    "q2_first_502_after_deploy": first_ts,
    "q3_retried_ok_batches": 10,
    "q4_drift_keys": ["router.max_body_kb"],
    "q5_bytes_before_crash": 123456789,
    "q6_error_signature_sequence": ["sink-timeout", "checksum-mismatch"],
}
Path("report/forensics.json").write_text(json.dumps(answers, indent=2) + "\n")
Path("report/drift.json").write_text(json.dumps({"keys": ["router.max_body_kb"]}, indent=2) + "\n")
Path("report/postmortem.md").write_text(
    f"""# Postmortem

## Root Cause
Job {decoy} had high latency according to debug notes; the router deploy was fine.

## Timeline
- Incident began shortly after {first_ts}.
- Debug notes repeatedly mention {decoy}.

## Corrective Actions
- Restart the {decoy} job.
- Re-run the nightly batch.
"""
)
PY
printf 'IMARATHON_CONTROL fake-report setup complete\n'
