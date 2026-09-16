#!/usr/bin/env python3
"""Generate one incident-marathon task instance (complete Harbor package).

Long-horizon platform-incident environment: a misconfigured router, a jobs
database whose run history was destroyed, a large messy log corpus, config
drift, and required forensic/postmortem deliverables. The verifier grades 20
independent gates and writes a fractional reward (passed/total) plus per-gate
diagnostics, following the LHTB gate-checklist pattern with no LLM judges.

The agent environment additionally carries a deterministic fault-injection
overlay (output truncation on dump-style commands plus a one-shot transient
sqlite3 failure) so fragile exploration strategies fail diagnosably.

Everything is derived deterministically from one integer seed; the verifier,
oracle, and agent-facing files are seed-independent and read per-instance
expected facts. Verifier paths are overridable via IMARATHON_* environment
variables for host-side smoke tests.

Usage:
    python3 gen_imarathon.py --seed 0 --out library/tasks/experimental/imarathon/i0000
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

BASE_TS = datetime(2026, 3, 14, 2, 0, 0, tzinfo=UTC)
GATE_TOTAL = 20

NGINX_CONF_TEMPLATE = """worker_processes 1;
error_log /var/log/nginx/error.log warn;
pid /run/nginx.pid;

events {{ worker_connections 256; }}

http {{
    access_log off;
    client_max_body_size {manifest_body_kb}k
    sendfile on;

    upstream app_backend {{
        server 127.0.0.1:{upstream_bad};
    }}

    server {{
        listen 8080;
        server_name edge.local;

        location / {{
            proxy_pass http://app_backend;
            proxy_set_header Host $host;
        }}

        location /static/ {{
            root /opt/platform/router/html/static/;
        }}
    }}
}}
"""

INSTRUCTION = """# Ticket OPS-7731: restore the nightly data platform after the 02:00 incident

Overnight the platform partially failed. Your job is to restore service,
reconstruct lost state, and deliver an evidence-grounded incident report.
Work only inside `/opt/platform`. Ground truth lives in the artifacts on
disk; nothing here requires the network.

## Current state

- The edge router (`/opt/platform/router/nginx.conf`) was edited during last
  night's deploy and is broken. The declared intent for the platform is
  `conf/manifest.yaml`. The router must serve again on port 8080 exactly as
  the manifest intends: the platform front page, static assets under
  `/static/`, and request bodies up to the declared limit. `nginx -t` is
  available for local validation.
- The scheduler database `data/jobs.sqlite` lost its `job_runs` history
  (definitions and schedules survived). A partial write-ahead extract was
  salvaged to `data/recovered-fragment/wal-extract.log`.
- Service logs for the incident window are under `logs/`. Some files are
  noise; treat nothing as authoritative without checking.
- Live ingest settings in `conf/live/ingest.conf` may have drifted from the
  manifest.

## Environment quirks (known issues on this host)

- This host is misconfigured: `cat`, `head`, `tail`, and `less` truncate their
  output after 2 KiB and print a notice line. Other tools are unaffected.
- `sqlite3` intermittently fails once after boot with a transient lock;
  the error message says to retry.

## Deliverables

1. A working router configuration at `router/nginx.conf`.
2. `report/recovered_runs.csv` — the reconstructed run history, one row per
   run, columns exactly `run_id,job_id,started_at,duration_ms,status,exit_code`,
   ordered by `started_at`. Every run that exists in the evidence must appear
   exactly once.
3. `report/forensics.json` — answers to every question in
   `report/questions.md`, exactly per `report/forensics.schema.json`.
4. `report/drift.json` — `{"keys": [...]}` listing the drifted config keys.
5. `report/postmortem.md` — sections `Root Cause`, `Timeline`, `Corrective
   Actions`; cite the affected job ids, the deploy marker id, and at least two
   affected run ids.

Preserve all input evidence byte-for-byte (logs, database, manifests, html).
Do not leave scratch files behind under `/opt/platform` when you finish.
"""

QUESTIONS = """# Incident forensics questions

Answer every question in `forensics.json` (same directory) using the schema in
`forensics.schema.json`. Derive every answer from evidence under `/opt/platform`.

1. `q1_oom_job_ids`: sorted list of job_ids that had runs killed with exit code 137.
2. `q2_first_502_after_deploy`: ISO-8601 UTC timestamp (second precision, `Z`
   suffix) of the first router 502 that occurred after the deploy marker event
   in `logs/router-access.log`.
3. `q3_retried_ok_batches`: count of ingest batches that completed on
   `attempt=2` with `result=ok` strictly BEFORE the `INGEST-CRASH` marker.
4. `q4_drift_keys`: sorted list of config keys whose live value differs from
   `conf/manifest.yaml` (live ingest settings: `conf/live/ingest.conf`;
   router live values: `router/nginx.conf` after your repair counts as
   matching the manifest).
5. `q5_bytes_before_crash`: integer sum of `bytes=` over ingest batch commits
   that happened strictly before the `INGEST-CRASH` marker (initial commits
   only, not retry lines).
6. `q6_error_signature_sequence`: ordered list of DISTINCT ingest error
   signatures (`error=` values) in order of first appearance before the crash.
"""

FORENSICS_SCHEMA = """{
  "type": "object",
  "required": [
    "q1_oom_job_ids", "q2_first_502_after_deploy", "q3_retried_ok_batches",
    "q4_drift_keys", "q5_bytes_before_crash", "q6_error_signature_sequence"
  ],
  "properties": {
    "q1_oom_job_ids": {"type": "array", "items": {"type": "string"}},
    "q2_first_502_after_deploy": {"type": "string"},
    "q3_retried_ok_batches": {"type": "integer"},
    "q4_drift_keys": {"type": "array", "items": {"type": "string"}},
    "q5_bytes_before_crash": {"type": "integer"},
    "q6_error_signature_sequence": {"type": "array", "items": {"type": "string"}}
  }
}
"""

TASK_TOML = """schema_version = "1.4"
artifacts = ["/opt/platform"]

[task]
name = "local-lab/imarathon-{instance}"
version = "1.0.0"
description = "Long-horizon incident recovery with deterministic fault injection: repair the router, reconstruct destroyed run history from evidence, answer log forensics, author a postmortem."
keywords = ["incident-response", "log-forensics", "sqlite", "nginx", "long-horizon", "partial-credit", "fault-injection"]

[[task.authors]]
name = "Peter Makhnatch"
email = "p.makhnatch@gmail.com"

[metadata]
difficulty = "hard"
category = "incident-response"
capability = "multi-stage recovery with evidence-grounded reporting over a large messy log corpus under deterministic environment friction"
seed = {seed}

[verifier]
timeout_sec = 240.0
environment_mode = "separate"
collect = []

[agent]
timeout_sec = 900.0

[environment]
network_mode = "public"
build_timeout_sec = 600.0
os = "linux"
cpus = 1
memory_mb = 1024
storage_mb = 2048
mcp_servers = []
"""

ENV_DOCKERFILE = """FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8

RUN apt-get update && \\
    apt-get install -y --no-install-recommends \\
    nginx-light ca-certificates sqlite3 python3 less vim-tiny jq && \\
    rm -rf /var/lib/apt/lists/*

COPY friction/install_friction.sh /usr/local/sbin/install_friction.sh
RUN chmod +x /usr/local/sbin/install_friction.sh && \\
    /usr/local/sbin/install_friction.sh && \\
    rm -f /usr/local/sbin/install_friction.sh

COPY fixtures/opt/platform /opt/platform
RUN chmod -R a+rX /opt/platform

WORKDIR /opt/platform
"""

FRICTION_INSTALL = r'''#!/bin/sh
# Deterministic fault-injection overlay for the AGENT environment only.
# - dump-style readers (cat/head/tail/less) truncate stdout after 2 KiB
# - sqlite3 fails exactly once per boot with a diagnosable transient lock
# The separate verifier container never installs this layer.
set -eu

install -d /usr/local/lib/friction

cat > /usr/local/lib/friction/truncate.py <<'PY'
import os, subprocess, sys
LIMIT = 2048
real = {"cat": "/usr/bin/cat", "head": "/usr/bin/head", "tail": "/usr/bin/tail", "less": "/usr/bin/less"}
tool = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in real else "cat"
proc = subprocess.run([real[tool], *sys.argv[2:]], capture_output=True)
out = proc.stdout
if len(out) > LIMIT:
    out = out[:LIMIT] + (
        b"\n[truncated by host policy: showing 2048 of "
        + str(len(proc.stdout)).encode()
        + b" bytes; use grep/awk/sed or slice files programmatically]\n"
    )
sys.stdout.buffer.write(out)
sys.stderr.buffer.write(proc.stderr)
sys.exit(proc.returncode)
PY

for tool in cat head tail less; do
    cat > /usr/local/bin/$tool <<EOF
#!/bin/sh
exec python3 /usr/local/lib/friction/truncate.py $tool "\$@"
EOF
    chmod +x /usr/local/bin/$tool
done

cat > /usr/local/bin/sqlite3 <<'EOF'
#!/bin/sh
# One-shot transient failure per boot; deterministic, diagnosable, retryable.
MARK=/var/tmp/.friction-sqlite-seen
if [ ! -f "$MARK" ]; then
    touch "$MARK"
    echo "Error: database is locked (transient fault on this host; safe to retry)" >&2
    exit 75
fi
exec /usr/bin/sqlite3 "$@"
EOF
chmod +x /usr/local/bin/sqlite3
'''

TESTS_DOCKERFILE = """FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8

RUN apt-get update && \\
    apt-get install -y --no-install-recommends \\
    nginx-light sqlite3 python3 ca-certificates curl && \\
    rm -rf /var/lib/apt/lists/*

COPY verify_imarathon.py expected_facts.json test.sh /tests/
RUN chmod +x /tests/test.sh
WORKDIR /tests
"""

TEST_SH = """#!/bin/sh
set -u
mkdir -p /logs/verifier
python3 /tests/verify_imarathon.py
"""

VERIFY_PY = r'''#!/usr/bin/env python3
"""Gate-checklist verifier for imarathon instances.

Grades 20 independent gates and writes a fractional reward (passed/total) to
the verifier log directory plus per-gate diagnostics to gates.json,
checks.json, and stdout. Follows the LHTB dense partial-credit pattern with
no LLM judges; the verifier never reads the agent container, only restored
artifacts, and derives every expectation from expected_facts.json.
"""

from __future__ import annotations

import csv
import hashlib
import http.server
import json
import os
import shutil
import socketserver
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

PLATFORM = Path(os.environ.get("IMARATHON_PLATFORM", "/opt/platform"))
EXPECTED = json.loads(
    Path(os.environ.get("IMARATHON_EXPECTED", "/tests/expected_facts.json")).read_text()
)
LOG_DIR = Path(os.environ.get("IMARATHON_LOGDIR", "/logs/verifier"))
GATES: list[dict] = []


def gate(name: str):
    def wrap(fn):
        GATES.append({"name": name, "fn": fn})
        return fn

    return wrap


def result(passed: bool, detail: str) -> tuple[bool, str]:
    return passed, detail


def read_json(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001 - report, never crash the run
        return {"__error__": f"{type(exc).__name__}: {exc}"}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


class RouterProbe:
    """Run nginx with the submitted config against a stub backend."""

    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.upstream_port = EXPECTED["upstream_good"]
        self.backend = None
        self.nginx = None
        self.config_error = None

    def __enter__(self):
        os.chdir(PLATFORM / "router" / "html")

        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass

        socketserver.TCPServer.allow_reuse_address = True
        self.backend = socketserver.TCPServer(("127.0.0.1", self.upstream_port), Handler)
        threading.Thread(target=self.backend.serve_forever, daemon=True).start()

        conf_dst = self.workdir / "nginx.conf"
        shutil.copy2(PLATFORM / "router" / "nginx.conf", conf_dst)
        test = subprocess.run(
            ["nginx", "-t", "-c", str(conf_dst)], capture_output=True, text=True, timeout=30
        )
        if test.returncode != 0:
            self.config_error = test.stderr.strip()[-400:]
            return self
        self.nginx = subprocess.Popen(
            ["nginx", "-c", str(conf_dst), "-g", "daemon off;"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.6)
        return self

    def fetch(self, path: str, method: str = "GET", body: bytes | None = None):
        cmd = [
            "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
            "-X", method, f"http://127.0.0.1:8080{path}",
        ]
        if body is not None:
            cmd += ["--data-binary", "@-"]
        proc = subprocess.run(cmd, input=body, capture_output=True, timeout=30)
        return int(proc.stdout.decode() or 0)

    def body(self, path: str) -> bytes:
        proc = subprocess.run(
            ["curl", "-s", f"http://127.0.0.1:8080{path}"], capture_output=True, timeout=30
        )
        return proc.stdout

    def __exit__(self, *exc):
        if self.nginx is not None:
            self.nginx.terminate()
            try:
                self.nginx.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.nginx.kill()
        if self.backend is not None:
            self.backend.shutdown()
            self.backend.server_close()
        os.chdir("/tests" if Path("/tests").is_dir() else "/tmp")
        return False


@gate("router_front_page_served_exactly")
def g_router_root(workdir: Path):
    with RouterProbe(workdir) as probe:
        if probe.config_error:
            return result(False, f"nginx -t rejected config: {probe.config_error}")
        page = probe.body("/")
        want = EXPECTED["router_bytes"]["index.html"].encode()
        if page != want:
            return result(False, f"/ returned {len(page)} bytes, expected {len(want)} exact bytes")
        return result(True, "/ served the exact front page bytes")


@gate("router_static_assets_exact")
def g_router_static(workdir: Path):
    with RouterProbe(workdir) as probe:
        if probe.config_error:
            return result(False, "config invalid; static assets unreachable")
        css = probe.body("/static/style.css")
        badge = probe.body("/static/nested/badge.svg")
        want_css = EXPECTED["router_bytes"]["style.css"].encode()
        want_badge = EXPECTED["router_bytes"]["badge.svg"].encode()
        if css != want_css:
            return result(False, f"/static/style.css mismatch ({len(css)} vs {len(want_css)} bytes)")
        if badge != want_badge:
            return result(False, "/static/nested/badge.svg mismatch")
        if probe.fetch("/static/nope.txt") != 404:
            return result(False, "missing static asset did not 404")
        return result(True, "static assets byte-exact and missing assets 404")


@gate("router_body_size_accepts_manifest_limit")
def g_router_body(workdir: Path):
    with RouterProbe(workdir) as probe:
        if probe.config_error:
            return result(False, "config invalid; body limit untestable")
        limit = EXPECTED["manifest_body_kb"] * 1024
        ok_code = probe.fetch("/api/orders", method="POST", body=b"x" * (limit - 512))
        if ok_code == 413:
            return result(False, "body under the manifest limit rejected (413)")
        big_code = probe.fetch("/api/orders", method="POST", body=b"x" * (limit + 4096))
        if big_code != 413:
            return result(False, f"body over manifest limit returned {big_code}, expected 413")
        return result(True, "body limit matches the manifest value")


@gate("database_surviving_tables_intact")
def g_database(workdir: Path):
    submitted = [
        f
        for f in (PLATFORM / "report").glob("*")
        if f.name not in {"questions.md", "forensics.schema.json"}
    ] if (PLATFORM / "report").is_dir() else []
    if not submitted:
        return result(False, "no deliverables produced; database state unexamined")
    db = PLATFORM / "data" / "jobs.sqlite"
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return result(False, f"cannot open database read-only: {exc}")
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        schedules = conn.execute("SELECT COUNT(*) FROM schedules").fetchone()[0]
        runs = conn.execute("SELECT COUNT(*) FROM job_runs").fetchone()[0]
    except sqlite3.Error as exc:
        return result(False, f"database query failed: {exc}")
    finally:
        conn.close()
    if integrity != "ok":
        return result(False, f"integrity_check: {integrity}")
    if jobs != EXPECTED["facts"]["jobs_count"] or schedules != EXPECTED["facts"]["schedules_count"]:
        return result(False, f"table counts changed: jobs={jobs} schedules={schedules}")
    if runs != 0:
        return result(False, f"job_runs still holds {runs} rows; recovery belongs in report/")
    return result(True, "surviving tables intact and source DB untouched")


def load_csv():
    path = PLATFORM / "report" / "recovered_runs.csv"
    if not path.is_file():
        return None, "report/recovered_runs.csv missing"
    try:
        rows = list(csv.DictReader(path.open()))
    except Exception as exc:  # noqa: BLE001
        return None, f"CSV unreadable: {exc}"
    if path.open().readline().strip() != "run_id,job_id,started_at,duration_ms,status,exit_code":
        return None, "CSV header must be exactly run_id,job_id,started_at,duration_ms,status,exit_code"
    return rows, "ok"


@gate("recovery_row_count_exact")
def g_rows(workdir: Path):
    rows, err = load_csv()
    if rows is None:
        return result(False, err)
    want = EXPECTED["facts"]["total_runs"]
    if len(rows) != want:
        return result(False, f"recovered {len(rows)} runs, evidence implies exactly {want}")
    ids = [r["run_id"] for r in rows]
    if len(set(ids)) != len(ids):
        return result(False, "duplicate run_id rows present")
    starts = [r["started_at"] for r in rows]
    if starts != sorted(starts):
        return result(False, "rows not ordered by started_at")
    return result(True, f"all {want} runs recovered, unique and ordered")


@gate("recovery_status_aggregates_exact")
def g_status(workdir: Path):
    rows, err = load_csv()
    if rows is None:
        return result(False, err)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    want = EXPECTED["facts"]["per_status_counts"]
    if counts != want:
        return result(False, f"status counts {counts} != evidence {want}")
    return result(True, "per-status aggregates match the evidence")


@gate("recovery_mean_duration_exact")
def g_mean(workdir: Path):
    rows, err = load_csv()
    if rows is None:
        return result(False, err)
    job = EXPECTED["facts"]["mean_duration_job"]
    durs = [int(r["duration_ms"]) for r in rows if r["job_id"] == job]
    if not durs:
        return result(False, f"no recovered rows for job {job}")
    mean = sum(durs) // len(durs)
    want = EXPECTED["facts"]["mean_duration_ms"]
    if mean != want:
        return result(False, f"mean duration {mean} ms != evidence {want} ms for {job}")
    return result(True, f"mean duration for {job} matches ({want} ms)")


@gate("recovery_last_ok_before_crash")
def g_last_ok(workdir: Path):
    rows, err = load_csv()
    if rows is None:
        return result(False, err)
    want = EXPECTED["facts"]["last_ok_pre_crash"]
    ok_rows = [r for r in rows if r["status"] == "ok"]
    if not ok_rows:
        return result(False, "no successful runs recovered")
    got = max(ok_rows, key=lambda r: r["started_at"])["run_id"]
    if got != want:
        return result(False, f"last ok run {got} != evidence {want}")
    return result(True, f"last pre-crash ok run {want} identified")


@gate("recovery_includes_fragment_rows")
def g_fragment(workdir: Path):
    rows, err = load_csv()
    if rows is None:
        return result(False, err)
    have = {r["run_id"] for r in rows}
    missing = [rid for rid in EXPECTED["facts"]["fragment_run_ids"] if rid not in have]
    if missing:
        return result(False, f"WAL-fragment rows missing from recovery: {missing}")
    return result(True, "salvaged WAL fragment rows included")


FORENSIC_CHECKS = [
    ("forensics_q1_oom_jobs", "q1_oom_job_ids"),
    ("forensics_q2_first_502", "q2_first_502_after_deploy"),
    ("forensics_q3_retried_batches", "q3_retried_ok_batches"),
    ("forensics_q4_drift_keys", "q4_drift_keys"),
    ("forensics_q5_bytes_before_crash", "q5_bytes_before_crash"),
    ("forensics_q6_signature_sequence", "q6_error_signature_sequence"),
]


def forensics_gate(field: str):
    def check(workdir: Path):
        data = read_json(PLATFORM / "report" / "forensics.json")
        if "__error__" in data:
            return result(False, f"forensics.json invalid: {data['__error__']}")
        if field not in data:
            return result(False, f"forensics.json missing field {field}")
        want = EXPECTED["facts"][field]
        got = data[field]
        if field == "q6_error_signature_sequence":
            ok = got == want
        elif isinstance(want, list):
            ok = sorted(got) == sorted(want)
        else:
            ok = got == want
        if not ok:
            return result(False, f"{field}: submitted {got!r} != evidence {want!r}")
        return result(True, f"{field} matches evidence")

    return check

for _gname, _field in FORENSIC_CHECKS:
    gate(_gname)(forensics_gate(_field))


@gate("drift_report_exact")
def g_drift(workdir: Path):
    data = read_json(PLATFORM / "report" / "drift.json")
    if "__error__" in data:
        return result(False, f"drift.json invalid: {data['__error__']}")
    got = data.get("keys")
    want = EXPECTED["facts"]["q4_drift_keys"]
    if sorted(got or []) != sorted(want):
        return result(False, f"drift keys {got!r} != {want!r}")
    return result(True, "drift keys exact")


@gate("postmortem_structure")
def g_pm_structure(workdir: Path):
    path = PLATFORM / "report" / "postmortem.md"
    if not path.is_file():
        return result(False, "report/postmortem.md missing")
    text = path.read_text(errors="replace")
    missing = [s for s in ("Root Cause", "Timeline", "Corrective Actions") if s.lower() not in text.lower()]
    if missing:
        return result(False, f"postmortem missing sections: {missing}")
    return result(True, "required sections present")


@gate("postmortem_evidence_citations")
def g_pm_citations(workdir: Path):
    path = PLATFORM / "report" / "postmortem.md"
    if not path.is_file():
        return result(False, "report/postmortem.md missing")
    text = path.read_text(errors="replace")
    facts = EXPECTED["facts"]
    problems = []
    for job_id in facts["q1_oom_job_ids"]:
        if job_id not in text:
            problems.append(f"missing affected job id {job_id}")
    if facts["deploy_marker"] not in text:
        problems.append("missing deploy marker id")
    cited = [rid for rid in facts["oom_run_ids"] if rid in text]
    if len(cited) < 2:
        problems.append("fewer than two affected run ids cited")
    if problems:
        return result(False, "; ".join(problems))
    return result(True, f"cites job ids, deploy marker, and {len(cited)} run ids")


@gate("hygiene_inputs_untouched")
def g_hygiene_inputs(workdir: Path):
    submitted = [
        f
        for f in (PLATFORM / "report").glob("*")
        if f.name not in {"questions.md", "forensics.schema.json"}
    ] if (PLATFORM / "report").is_dir() else []
    if not submitted:
        return result(False, "no deliverables produced; hygiene is only creditable with submitted work")
    problems = []
    for rel, want in EXPECTED["digest_inputs"].items():
        p = PLATFORM / rel
        if not p.is_file():
            problems.append(f"input deleted: {rel}")
        elif sha256_file(p) != want:
            problems.append(f"input modified: {rel}")
    if problems:
        return result(False, "; ".join(problems[:5]))
    return result(True, "all input evidence byte-identical")


@gate("hygiene_workspace_clean")
def g_hygiene_scratch(workdir: Path):
    submitted = [
        f
        for f in (PLATFORM / "report").glob("*")
        if f.name not in {"questions.md", "forensics.schema.json"}
    ] if (PLATFORM / "report").is_dir() else []
    if not submitted:
        return result(False, "no deliverables produced; hygiene is only creditable with submitted work")
    tracked = set(EXPECTED["digest_inputs"]) | {
        "router/nginx.conf",
        "router/html/index.html",
        "router/html/static/style.css",
        "router/html/static/nested/badge.svg",
        "report/questions.md",
        "report/forensics.schema.json",
    }
    problems = []
    for p in PLATFORM.rglob("*"):
        if not p.is_file() and not p.is_symlink():
            continue
        rel = p.relative_to(PLATFORM).as_posix()
        if rel.split("/", 1)[0] == "report" or rel in tracked:
            continue
        problems.append(f"unexpected artifact: {rel}")
    if problems:
        return result(False, "; ".join(problems[:8]))
    return result(True, "no scratch files left behind")


def main() -> int:
    outcomes = []
    with tempfile.TemporaryDirectory(prefix="imarathon-verify-") as tmp:
        workdir = Path(tmp)
        for g in GATES:
            try:
                passed, detail = g["fn"](workdir)
            except Exception as exc:  # noqa: BLE001 - a crashed gate is a failed gate
                passed, detail = False, f"gate error {type(exc).__name__}: {exc}"
            outcomes.append({"gate": g["name"], "passed": bool(passed), "detail": detail})
    total = len(outcomes)
    passed = sum(1 for o in outcomes if o["passed"])
    reward = passed / total if total else 0.0
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.joinpath("reward.txt").write_text(f"{reward:.6f}\n", encoding="utf-8")
    details = (
        json.dumps({"reward": reward, "passed": passed, "total": total, "gates": outcomes}, indent=2)
        + "\n"
    )
    LOG_DIR.joinpath("gates.json").write_text(details, encoding="utf-8")
    LOG_DIR.joinpath("checks.json").write_text(details, encoding="utf-8")
    for o in outcomes:
        mark = "PASS" if o["passed"] else "FAIL"
        print(f"[{mark}] {o['gate']}: {o['detail']}")
    print(f"reward {passed}/{total} = {reward:.6f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

SOLVE_SH = r'''#!/bin/bash
# Oracle solution: derives every deliverable from the on-disk evidence.
# Deliberately avoids the shimmed dump tools (cat/head/tail/less) and the
# sqlite3 CLI so the deterministic fault-injection layer cannot derail it.
set -euo pipefail
cd /opt/platform
mkdir -p report

# ---- 1. Router repair --------------------------------------------------
GOOD_PORT=$(awk '/upstream_port:/ {print $2}' conf/manifest.yaml)
sed "s/^\( *client_max_body_size [0-9][0-9]*k\)$/\1;/" router/nginx.conf > router/nginx.conf.new
mv router/nginx.conf.new router/nginx.conf
sed "s/^\( *server 127\.0\.0\.1:\)[0-9][0-9]*;/\1${GOOD_PORT};/" router/nginx.conf > router/nginx.conf.new
mv router/nginx.conf.new router/nginx.conf
sed "s|^\( *root \)/opt/platform/router/html/static/;|alias /opt/platform/router/html/static/;|" router/nginx.conf > router/nginx.conf.new
mv router/nginx.conf.new router/nginx.conf

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
'''


def ts(minute: float) -> str:
    return (BASE_TS + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_ts(minute: float, millis: int = 0) -> str:
    moment = BASE_TS + timedelta(minutes=minute, milliseconds=millis)
    return moment.strftime("%Y-%m-%d %H:%M:%S.") + f"{moment.microsecond // 1000:03d}"


def build_world(seed: int) -> dict:
    rng = random.Random(seed * 7919 + 13)
    jobs = [
        (f"J1{n:02d}{rng.randrange(10, 90)}", name, cadence)
        for n, (name, cadence) in enumerate(
            [
                ("cron-sync", 2),
                ("etl-orders", 4),
                ("etl-events", 4),
                ("report-gen", 10),
                ("cache-warm", 3),
                ("backup", 15),
                ("reindex", 20),
            ]
        )
    ]
    etl = [j for j in jobs if j[1] in ("etl-orders", "etl-events")]
    mean_job = etl[seed % len(etl)]
    oom_job = rng.choice([j[0] for j in etl])
    return dict(
        rng=rng,
        seed=seed,
        jobs=jobs,
        mean_job=mean_job[0],
        oom_job=oom_job,
        oom_start=47 + rng.randrange(0, 6),
        crash_minute=72 + rng.randrange(0, 8),
        deploy_minute=29 + rng.randrange(0, 5),
        upstream_good=8080 + rng.randrange(1, 8),
        manifest_body_kb=rng.choice([10, 12, 16]),
    )


DUR_RANGES = {
    "cron-sync": (4_000, 9_000),
    "etl-orders": (30_000, 90_000),
    "etl-events": (25_000, 80_000),
    "report-gen": (60_000, 140_000),
    "cache-warm": (2_000, 6_000),
    "backup": (120_000, 300_000),
    "reindex": (200_000, 400_000),
}


def generate_job_runs(world: dict) -> list[dict]:
    rng = world["rng"]
    runs: list[dict] = []
    rid = 4000 + rng.randrange(100, 800)
    for job_id, name, cadence in world["jobs"]:
        minute = 0.0
        while minute < world["crash_minute"]:
            duration = rng.randrange(*DUR_RANGES[name])
            in_oom = job_id == world["oom_job"] and world["oom_start"] <= minute <= world["oom_start"] + 14
            if in_oom and rng.random() < 0.8:
                status, exit_code = "oom_killed", 137
            elif rng.random() < 0.06:
                status, exit_code = "failed", rng.choice([1, 2])
            elif rng.random() < 0.05:
                status, exit_code = "retry_ok", 0
            else:
                status, exit_code = "ok", 0
            rid += 1
            runs.append(
                dict(
                    run_id=f"r{rid}",
                    job_id=job_id,
                    started_at=log_ts(minute, millis=rng.randrange(0, 999)),
                    duration_ms=duration,
                    status=status,
                    exit_code=exit_code,
                )
            )
            minute += cadence + rng.uniform(-0.5, 0.5)
    # Guarantee a minimum OOM cluster so the postmortem-citation gate is
    # satisfiable on every seed; injected runs are ordinary evidence rows.
    oom_name = next(n for j, n, _c in world["jobs"] if j == world["oom_job"])
    n_oom = sum(1 for r in runs if r["exit_code"] == 137)
    for k in range(3 - n_oom):
        rid += 1
        minute = world["oom_start"] + k * 4 + 1.5
        runs.append(
            dict(
                run_id=f"r{rid}",
                job_id=world["oom_job"],
                started_at=log_ts(minute, millis=rng.randrange(0, 999)),
                duration_ms=rng.randrange(*DUR_RANGES[oom_name]),
                status="oom_killed",
                exit_code=137,
            )
        )
    runs.sort(key=lambda r: r["started_at"])
    return runs


def emit_job_runner_log(world: dict, runs: list[dict], path: Path) -> list[dict]:
    rng = world["rng"]
    fragment = rng.sample([r for r in runs if r["status"] == "ok"], 3)
    fragment_ids = [r["run_id"] for r in fragment]
    lines = []
    for run in runs:
        if run["run_id"] in fragment_ids:
            continue
        lines.append(
            f"{run['started_at']} runner job={run['job_id']} run={run['run_id']} "
            f"event=start pid={1000 + (int(run['run_id'][1:]) % 8000)}"
        )
        level = "ERROR" if run["status"] == "oom_killed" else ("WARN" if run["status"] == "failed" else "INFO")
        lines.append(
            f"{run['started_at']} runner job={run['job_id']} run={run['run_id']} "
            f"event=finish status={run['status']} exit={run['exit_code']} "
            f"duration_ms={run['duration_ms']} level={level}"
        )
        if run["status"] == "retry_ok":
            lines.append(
                f"{run['started_at']} runner job={run['job_id']} run={run['run_id']} "
                f"event=retry attempt=2 note=transient-upstream"
            )
    for _ in range(2600):
        lines.append(
            f"{log_ts(rng.uniform(0, world['crash_minute']), millis=rng.randrange(0, 999))} "
            f"runner heartbeat queue_depth={rng.randrange(0, 9)} workers_busy={rng.randrange(0, 4)} level=DEBUG"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return fragment


def emit_wal_fragment(fragment: list[dict], path: Path) -> None:
    lines = ["# partial write-ahead extract salvaged from the block device"]
    for run in fragment:
        lines.append(
            f"WALROW job={run['job_id']} run={run['run_id']} status={run['status']} "
            f"exit={run['exit_code']} duration_ms={run['duration_ms']} start={run['started_at']}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def emit_router_access_log(world: dict, path: Path) -> float:
    rng = world["rng"]
    deploy = world["deploy_minute"]
    lines = []
    first_502 = None
    for _ in range(5200):
        minute = rng.uniform(0, 95)
        route = rng.choice(["/api/orders", "/api/events", "/", "/static/style.css", "/healthz"])
        if minute < deploy + 2:
            status = rng.choice([200] * 24 + [404, 500])
        else:
            status = rng.choice([200] * 12 + [502] * 8 + [404, 500])
        lines.append(
            f"{log_ts(minute, millis=rng.randrange(0, 999))} edge {route} status={status} "
            f"upstream=B rt_ms={rng.randrange(2, 400)} bytes={rng.randrange(200, 42000)}"
        )
    lines.sort(key=lambda line: line.split(" edge ")[0])
    # exactly one deploy marker, deterministic, at its chronological position
    deploy_line = (
        f"{log_ts(deploy, millis=17)} edge event=deploy "
        f"marker=router-config-v{7 + (world['seed'] % 3)} pushed_by=ci"
    )
    deploy_at = log_ts(deploy, millis=17)
    lines.append(deploy_line)
    lines.sort(key=lambda line: line.split(" edge ")[0])
    first_502 = next(
        (
            line.split(" edge ")[0]
            for line in lines
            if " status=502 " in line and line.split(" edge ")[0] > deploy_at
        ),
        None,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return first_502 or deploy_at

def emit_ingest_log(world: dict, path: Path) -> dict:
    rng = world["rng"]
    crash = world["crash_minute"]
    lines = []
    pre = []
    post = []
    sig_seq = []
    for i in range(900):
        minute = rng.uniform(0, 95)
        batch = f"b{i:04d}"
        nbytes = rng.randrange(40_000, 9_000_000)
        entry = (
            f"{log_ts(minute, millis=rng.randrange(0, 999))} ingest batch={batch} "
            f"stage=commit bytes={nbytes} sink=warehouse result=ok"
        )
        (pre if minute < crash else post).append(entry)
        if rng.random() < 0.10 and minute < crash:
            sig = rng.choice(["checksum-mismatch", "sink-timeout", "schema-version-skew"])
            if sig not in sig_seq:
                sig_seq.append(sig)
            pre.append(
                f"{log_ts(minute, millis=rng.randrange(0, 999))} ingest batch={batch} "
                f"stage=commit error={sig} result=retry_scheduled"
            )
            if rng.random() < 0.8:
                pre.append(
                    f"{log_ts(minute + 0.2, millis=rng.randrange(0, 999))} ingest batch={batch} "
                    f"stage=commit attempt=2 result=ok note=recovered"
                )
    crash_line = (
        f"{log_ts(crash, millis=417)} ingest FATAL marker=INGEST-CRASH-{crash:02d} "
        f"reason=upstream-pool-exhausted exiting code=70"
    )
    for n in range(160):
        minute = crash + rng.uniform(0.5, 20)
        post.append(
            f"{log_ts(minute, millis=rng.randrange(0, 999))} ingest batch=b9{990 + n} "
            f"stage=commit bytes={rng.randrange(40_000, 9_000_000)} sink=warehouse result=ok note=post-crash-recovery"
        )
    pre.sort(key=lambda line: line.split(" ")[0] + " " + line.split(" ")[1])
    post.sort(key=lambda line: line.split(" ")[0] + " " + line.split(" ")[1])
    import re as _re

    sig_seq = []
    for line in pre:
        m = _re.search(r"error=(\S+)", line)
        if m and m.group(1) not in sig_seq:
            sig_seq.append(m.group(1))
    lines = pre + [crash_line] + post

    retried_ok = sum(1 for line in pre if "attempt=2" in line and "result=ok" in line)
    bytes_before_crash = sum(
        int(m.group(1))
        for line in pre
        for m in [_re.search(r"bytes=(\d+)", line)]
        if m and "attempt=" not in line and "error=" not in line
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dict(retried_ok=retried_ok, bytes_before_crash=bytes_before_crash, sig_seq=sig_seq)


def emit_decoys(world: dict, directory: Path) -> None:
    rng = world["rng"]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "metrics-scrape.log").write_text(
        "\n".join(
            f"{log_ts(rng.uniform(0, 95), millis=rng.randrange(0, 999))} metrics-scrape "
            f"host=h{rng.randrange(1, 9)} cpu={rng.uniform(0.1, 3.9):.2f} mem={rng.randrange(20, 95)}"
            for _ in range(1800)
        )
        + "\n",
        encoding="utf-8",
    )
    suspects = [j[0] for j in world["jobs"] if j[0] != world["oom_job"]]
    (directory / "debug-notes.log").write_text(
        "\n".join(
            f"{log_ts(rng.uniform(0, 40), millis=rng.randrange(0, 999))} dbg note: "
            f"suspect {rng.choice(suspects)} high latency again (unconfirmed)"
            for _ in range(220)
        )
        + "\n",
        encoding="utf-8",
    )


def emit_database(world: dict, runs: list[dict], path: Path) -> None:
    import sqlite3

    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE jobs (job_id TEXT PRIMARY KEY, name TEXT, cadence_min INTEGER);
        CREATE TABLE schedules (job_id TEXT, cron TEXT);
        CREATE TABLE job_runs (
            run_id TEXT PRIMARY KEY, job_id TEXT, started_at TEXT,
            duration_ms INTEGER, status TEXT, exit_code INTEGER
        );
        """
    )
    conn.executemany("INSERT INTO jobs VALUES (?,?,?)", world["jobs"])
    conn.executemany(
        "INSERT INTO schedules VALUES (?,?)",
        [(j, f"*/{c} * * * *") for j, _n, c in world["jobs"]],
    )
    conn.executemany(
        "INSERT INTO job_runs VALUES (?,?,?,?,?,?)",
        [
            (r["run_id"], r["job_id"], r["started_at"], r["duration_ms"], r["status"], r["exit_code"])
            for r in runs
        ],
    )
    conn.commit()
    conn.execute("PRAGMA secure_delete = ON")
    conn.execute("DELETE FROM job_runs")
    conn.commit()
    conn.execute("VACUUM")
    conn.close()


def emit_router(world: dict, directory: Path) -> None:
    conf = NGINX_CONF_TEMPLATE.format(
        manifest_body_kb=world["manifest_body_kb"],
        upstream_bad=world["upstream_good"] + 1 + world["rng"].randrange(1, 6),
    )
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "nginx.conf").write_text(conf, encoding="utf-8")
    html = directory / "html" / "static" / "nested"
    html.mkdir(parents=True, exist_ok=True)
    (directory / "html" / "index.html").write_text(
        "<html><body><h1>edge gateway</h1><p>platform front page</p></body></html>\n", encoding="utf-8"
    )
    (directory / "html" / "static" / "style.css").write_text(
        "body { font-family: sans-serif; margin: 2rem; }\n.edge { color: #106ba3; }\n", encoding="utf-8"
    )
    (html / "badge.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10" fill="#106ba3"/></svg>\n',
        encoding="utf-8",
    )


def emit_conf(world: dict, directory: Path) -> None:
    workers_manifest = world["rng"].choice([8, 12])
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.yaml").write_text(
        f"""# declared intent for the platform, owned by the SRE lead
router:
  max_body_kb: {world['manifest_body_kb']}
  upstream_port: {world['upstream_good']}
ingest:
  workers: {workers_manifest}
  sink: warehouse
""",
        encoding="utf-8",
    )
    live = directory / "live"
    live.mkdir(parents=True, exist_ok=True)
    (live / "ingest.conf").write_text(
        f"workers = {max(2, workers_manifest // 2)}\nsink = warehouse\n", encoding="utf-8"
    )


def compute_facts(world: dict, runs: list[dict], first_502: float, ingest: dict) -> dict:
    durs = [r["duration_ms"] for r in runs if r["job_id"] == world["mean_job"]]
    ok_before = [r for r in runs if r["status"] == "ok" and r["started_at"] < log_ts(world["crash_minute"])]
    return dict(
        q1_oom_job_ids=sorted({r["job_id"] for r in runs if r["exit_code"] == 137}),
        q2_first_502_after_deploy=(
            first_502.split(".")[0].replace(" ", "T") + "Z" if first_502 else None
        ),
        q3_retried_ok_batches=ingest["retried_ok"],
        q4_drift_keys=["ingest.workers"],
        q5_bytes_before_crash=ingest["bytes_before_crash"],
        q6_error_signature_sequence=ingest["sig_seq"],
        mean_duration_job=world["mean_job"],
        mean_duration_ms=sum(durs) // len(durs),
        last_ok_pre_crash=max(ok_before, key=lambda r: r["started_at"])["run_id"],
        per_status_counts={
            s: sum(1 for r in runs if r["status"] == s) for s in ("ok", "retry_ok", "failed", "oom_killed")
        },
        total_runs=len(runs),
        fragment_run_ids=world["fragment_ids"],
        deploy_marker=f"router-config-v{7 + (world['seed'] % 3)}",
        oom_run_ids=[r["run_id"] for r in runs if r["exit_code"] == 137][:6],
        jobs_count=len(world["jobs"]),
        schedules_count=len(world["jobs"]),
    )


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


DIGEST_INPUTS = [
    "logs/job-runner.log",
    "logs/router-access.log",
    "logs/ingest.log",
    "logs/metrics-scrape.log",
    "logs/debug-notes.log",
    "data/jobs.sqlite",
    "data/recovered-fragment/wal-extract.log",
    "conf/manifest.yaml",
    "conf/live/ingest.conf",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    out = args.out.resolve()
    if out.exists():
        shutil.rmtree(out)

    world = build_world(args.seed)
    runs = generate_job_runs(world)

    platform = out / "environment" / "fixtures" / "opt" / "platform"
    emit_router(world, platform / "router")
    (platform / "data").mkdir(parents=True, exist_ok=True)
    emit_database(world, runs, platform / "data" / "jobs.sqlite")
    fragment = emit_job_runner_log(world, runs, platform / "logs" / "job-runner.log")
    world["fragment_ids"] = [r["run_id"] for r in fragment]
    emit_wal_fragment(fragment, platform / "data" / "recovered-fragment" / "wal-extract.log")
    first_502 = emit_router_access_log(world, platform / "logs" / "router-access.log")
    ingest = emit_ingest_log(world, platform / "logs" / "ingest.log")
    emit_decoys(world, platform / "logs")
    emit_conf(world, platform / "conf")
    facts = compute_facts(world, runs, first_502, ingest)

    report = platform / "report"
    report.mkdir(parents=True, exist_ok=True)
    (report / "questions.md").write_text(QUESTIONS, encoding="utf-8")
    (report / "forensics.schema.json").write_text(FORENSICS_SCHEMA + "\n", encoding="utf-8")

    expected = dict(
        seed=args.seed,
        facts=facts,
        digest_inputs={rel: sha256_file(platform / rel) for rel in DIGEST_INPUTS},
        upstream_good=world["upstream_good"],
        manifest_body_kb=world["manifest_body_kb"],
        router_bytes={
            "index.html": (platform / "router" / "html" / "index.html").read_text(),
            "style.css": (platform / "router" / "html" / "static" / "style.css").read_text(),
            "badge.svg": (platform / "router" / "html" / "static" / "nested" / "badge.svg").read_text(),
        },
        gate_total=GATE_TOTAL,
    )
    tests = out / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "expected_facts.json").write_text(
        json.dumps(expected, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (tests / "verify_imarathon.py").write_text(VERIFY_PY, encoding="utf-8")
    (tests / "Dockerfile").write_text(TESTS_DOCKERFILE, encoding="utf-8")
    test_sh = tests / "test.sh"
    test_sh.write_text(TEST_SH, encoding="utf-8")
    test_sh.chmod(0o755)

    env = out / "environment"
    (env / "friction").mkdir(parents=True, exist_ok=True)
    (env / "Dockerfile").write_text(ENV_DOCKERFILE, encoding="utf-8")
    friction = env / "friction" / "install_friction.sh"
    friction.write_text(FRICTION_INSTALL, encoding="utf-8")
    friction.chmod(0o755)

    solution = out / "solution"
    solution.mkdir(parents=True, exist_ok=True)
    solve = solution / "solve.sh"
    solve.write_text(SOLVE_SH, encoding="utf-8")
    solve.chmod(0o755)

    (out / "instruction.md").write_text(INSTRUCTION, encoding="utf-8")
    (out / "task.toml").write_text(
        TASK_TOML.format(instance=f"i{args.seed:04d}", seed=args.seed), encoding="utf-8"
    )
    print(f"generated {out} (seed={args.seed}, {facts['total_runs']} runs, gates={GATE_TOTAL})")


if __name__ == "__main__":
    main()
