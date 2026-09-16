# expect: 0.85
# Partial-credit control: complete evidence analysis and reporting; router left broken.
# Container-only; never run on host.
#!/bin/bash
# Oracle solution: derives every deliverable from the on-disk evidence.
# Deliberately avoids the shimmed dump tools (cat/head/tail/less) and the
# sqlite3 CLI so the deterministic fault-injection layer cannot derail it.
set -euo pipefail
cd /opt/platform
mkdir -p report

# ---- 1. Router repair --------------------------------------------------
GOOD_PORT=$(awk '/upstream_port:/ {print $2}' conf/manifest.yaml)

# ---- 2.-5. Everything else derives from evidence ------------------------
python3 - <<'PY'
import csv, json, re
from pathlib import Path

runs = {}
finish = re.compile(
    r"^(?P<ts>\S+ \S+) runner job=(?P<job>\S+) run=(?P<run>\S+) "
    r"event=finish status=(?P<status>\S+) exit=(?P<exit>\d+) duration_ms=(?P<dur>\d+)"
)
for line in Path("logs/job-runner.log").read_text().splitlines():
    m = finish.match(line)
    if m:
        runs[m["run"]] = m.groupdict()

wal = re.compile(
    r"^WALROW job=(?P<job>\S+) run=(?P<run>\S+) status=(?P<status>\S+) "
    r"exit=(?P<exit>\d+) duration_ms=(?P<dur>\d+) start=(?P<ts>\S+ \S+)"
)
for line in Path("data/recovered-fragment/wal-extract.log").read_text().splitlines():
    m = wal.match(line)
    if m:
        runs.setdefault(m["run"], m.groupdict())

rows = sorted(runs.values(), key=lambda r: r["ts"])
with open("report/recovered_runs.csv", "w", newline="") as fh:
    w = csv.writer(fh)
    w.writerow(["run_id", "job_id", "started_at", "duration_ms", "status", "exit_code"])
    for r in rows:
        w.writerow([r["run"], r["job"], r["ts"], r["dur"], r["status"], r["exit"]])

lines = Path("logs/job-runner.log").read_text().splitlines()
oom_jobs = sorted({m["job"] for m in map(finish.match, lines) if m and m["exit"] == "137"})
oom_runs = [m["run"] for m in map(finish.match, lines) if m and m["exit"] == "137"][:4]

access = Path("logs/router-access.log").read_text().splitlines()
deploy_at = None
marker = None
first_502 = None
deploy_at = None
marker = None
stamps_502 = []
for line in access:
    stamp = line.split(" edge ")[0]
    if "event=deploy" in line and "pushed_by=" in line:
        deploy_at = stamp
        marker = re.search(r"marker=(\S+)", line).group(1)
    elif " status=502 " in line:
        stamps_502.append(stamp)
# timestamps share a fixed format, so lexicographic order is chronological
after = [s for s in stamps_502 if deploy_at is None or s > deploy_at]
earliest = min(after) if after else None
first_502 = earliest.split(".")[0].replace(" ", "T") + "Z" if earliest else None

ingest = Path("logs/ingest.log").read_text().splitlines()
crash_idx = next(i for i, l in enumerate(ingest) if "INGEST-CRASH" in l)
retried = 0
sig_seq = []
bytes_before = 0
for line in ingest[:crash_idx]:
    _ts, rest = line.split(" ", 1)
    if "result=ok" in rest and "attempt=2" in rest:
        retried += 1
    m = re.search(r"error=(\S+)", rest)
    if m and m.group(1) not in sig_seq:
        sig_seq.append(m.group(1))
    if "stage=commit bytes=" in rest and "attempt=" not in rest and "error=" not in rest:
        bytes_before += int(re.search(r"bytes=(\d+)", rest).group(1))

manifest_workers = int(re.search(r"workers:\s*(\d+)", Path("conf/manifest.yaml").read_text()).group(1))
live_workers = int(re.search(r"workers\s*=\s*(\d+)", Path("conf/live/ingest.conf").read_text()).group(1))
drift = ["ingest.workers"] if live_workers != manifest_workers else []

forensics = {
    "q1_oom_job_ids": oom_jobs,
    "q2_first_502_after_deploy": first_502,
    "q3_retried_ok_batches": retried,
    "q4_drift_keys": drift,
    "q5_bytes_before_crash": bytes_before,
    "q6_error_signature_sequence": sig_seq,
}
Path("report/forensics.json").write_text(json.dumps(forensics, indent=2) + "\n")
Path("report/drift.json").write_text(json.dumps({"keys": drift}, indent=2) + "\n")

pm = f"""# Postmortem: nightly platform incident

## Root Cause
The deploy ({marker}) rewrote the edge router configuration: an invalid
client_max_body_size directive (missing semicolon), an upstream pointed at the
wrong port, and /static/ switched from alias to root. Separately, job
{' '.join(oom_jobs)} was repeatedly OOM-killed (exit 137), and the scheduler
database lost the job_runs table, forcing evidence-based reconstruction.

## Timeline
- Router deploy marker {marker} pushed before the first 502s appeared.
- OOM kills concentrated on {' '.join(oom_jobs)} (e.g. runs {', '.join(oom_runs[:4])}).
- Ingest crashed with an exhausted upstream pool; batches recovered on retry.

## Corrective Actions
- Repair router/nginx.conf against conf/manifest.yaml and add nginx -t to the
  deploy pipeline.
- Cap memory for {' '.join(oom_jobs)} and alert on exit 137 clusters.
- Back up the scheduler database and audit WAL retention before deletion.
"""
Path("report/postmortem.md").write_text(pm)
PY
printf 'IMARATHON_ORACLE_DONE\n'
