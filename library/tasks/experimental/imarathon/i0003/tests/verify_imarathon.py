#!/usr/bin/env python3
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
