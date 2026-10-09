"""Behavioural tests for the general judge-domain transforms.

G2 (general-pinned-backup@1): setup insertion, idempotence, refusal cases,
shell syntax of the inserted block.
G4 (general-strict-answer@1): fallback removal, idempotence, refusal cases,
and end-to-end evidence behavior of the patched ``_evidence_sc`` against a
stub ``_helpers.py`` (missing answer.md stays missing; other missing files
keep the renamed-file fallback).
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from evallab.general_pinned_backup import SNAPSHOT_BLOCK
from evallab.general_pinned_backup import build_changes as g2_changes
from evallab.general_pinned_backup import build_setup_sh as g2_setup
from evallab.general_strict_answer import build_changes as g4_changes
from evallab.general_strict_answer import build_verify_py as g4_verify
from evallab.task_variants import VariantInvalid

SETUP_TEMPLATE = """#!/bin/bash
M=/var/lib/mimo
[ -f "$M/ready" ] && exit 0
python3 "$M/files/fetch.py" "$M/files/fetch.json" || fail "fetch"
write_blocklist

touch "$M/ready"
echo "setup done"
"""

TOML_TEMPLATE = """schema_version = "1.4"
[task]
name = "mimo-v2.6-rl/s3k_test"
[environment]
workdir = "/work/workspace"
docker_image = "docker.io/xiaomimimo/mimo-v2.6-rl-oss@sha256:abc"
[environment.healthcheck]
command = "bash -c 'test -f /var/lib/mimo/ready || { mkdir -p /var/lib/mimo && echo __BLOB__ | base64 -d | tar -xzf - -C /var/lib/mimo && bash /var/lib/mimo/setup.sh; }'"
"""

VERIFY_TEMPLATE = """import argparse, json, os, subprocess, sys, tempfile
from pathlib import Path
HERE = Path(__file__).resolve().parent
def _evidence_sc(ws, files, budget=20000):
    import importlib.util, re as _re
    try:
        spec = importlib.util.spec_from_file_location("_th_ev", str(HERE / "_helpers.py"))
        th = importlib.util.module_from_spec(spec); spec.loader.exec_module(th)
    except Exception:
        return ""
    parts = []
    for f in files or []:
        p = ws / f
        if not p.exists():
            kws = [w for w in _re.split(r"[\u00b7\\s_./]+", Path(f).stem) if len(w) >= 2]
            try:
                hit = th.locate_deliverable(ws, *kws)
            except Exception:
                hit = None
            p = hit or p
        if p.exists():
            parts.append("===== " + p.name + " =====")
        else:
            parts.append("===== " + str(f) + " (\u6587\u4ef6\u7f3a\u5931) =====")
    return "\\n\\n".join(parts)
"""


@pytest.fixture()
def parent_dir(tmp_path: Path) -> Path:
    from evallab.strip_future_history import pack_setup  # noqa: PLC0415

    root = tmp_path / "parent"
    setup = root / "environment" / "setup"
    (setup / "files").mkdir(parents=True)
    (setup / "setup.sh").write_text(SETUP_TEMPLATE, encoding="utf-8")
    (setup / "files" / "blocklist").write_text("0.0.0.0 example.com\n", encoding="utf-8")
    (root / "tests" / "verifier").mkdir(parents=True)
    (root / "tests" / "verifier" / "verify.py").write_text(VERIFY_TEMPLATE, encoding="utf-8")
    blob = pack_setup(setup)
    (root / "task.toml").write_text(TOML_TEMPLATE.replace("__BLOB__", blob), encoding="utf-8")
    return root


# --- G2 -------------------------------------------------------------------- #




def test_g2_is_idempotent(parent_dir: Path) -> None:
    parent_text = (parent_dir / "environment/setup/setup.sh").read_text(encoding="utf-8")
    once = g2_setup(parent_text)
    assert g2_setup(once) == once


def test_g2_refuses_without_unique_sentinel() -> None:
    with pytest.raises(VariantInvalid, match="ready sentinel"):
        g2_setup('touch "$M/ready"\nmid\ntouch "$M/ready"\n')
    with pytest.raises(VariantInvalid, match="ready sentinel"):
        g2_setup("# no sentinel here\n")


def test_g2_only_setup_and_toml_change(parent_dir: Path) -> None:
    changes, _ = g2_changes(parent_dir)
    assert set(changes) == {"environment/setup/setup.sh", "task.toml"}
    assert not any(key.startswith("tests/") for key in changes)


def test_g2_inputs_record_task(parent_dir: Path) -> None:
    _, inputs = g2_changes(parent_dir)
    assert inputs["parent_task"] == "mimo-v2.6-rl/s3k_test"
    assert inputs["setup_before_sha256"] != inputs["setup_after_sha256"]




def test_g2_block_snapshots_and_fails_closed(tmp_path: Path) -> None:
    system = tmp_path / "system" / "vault"
    system.mkdir(parents=True)
    connection = sqlite3.connect(system / "state.db")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE records(value TEXT)")
    connection.execute("INSERT INTO records VALUES ('committed-in-wal')")
    connection.commit()
    # The block hardcodes /work/system; exercise its logic with the path rebound.
    script = SNAPSHOT_BLOCK.replace("/work/system", str(tmp_path / "system"))
    script = "fail() { echo \"setup: $*\" >&2; exit 1; }\n" + script + "\nexit 0\n"
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    with sqlite3.connect(system / "state.db.pinned_backup") as baseline:
        assert baseline.execute("SELECT value FROM records").fetchall() == [("committed-in-wal",)]
    connection.close()


def test_g2_block_missing_snapshot_fails(tmp_path: Path) -> None:
    system = tmp_path / "system" / "vault"
    system.mkdir(parents=True)
    (system / "state.db").write_bytes(b"not a sqlite database")
    check_only = SNAPSHOT_BLOCK.replace("/work/system", str(tmp_path / "system"))
    script = "fail() { echo \"setup: $*\" >&2; exit 1; }\n" + check_only + "\n"
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0
    assert "general-pinned-backup@1" in proc.stderr


def _exec_evidence(verify_src: str, helpers_src: str, ws: Path, files=None):
    helpers_path = ws.parent / "_helpers_pkg" / "_helpers.py"
    helpers_path.parent.mkdir(parents=True, exist_ok=True)
    helpers_path.write_text(helpers_src, encoding="utf-8")
    # Point HERE at the stub helpers directory.
    src = verify_src.replace('HERE = Path(__file__).resolve().parent',
                             f'HERE = Path({str(helpers_path.parent)!r})')
    namespace: dict = {"__name__": "verify_under_test"}
    exec(compile(src, "<verify.py>", "exec"), namespace)  # noqa: S102
    from pathlib import Path as _P  # noqa: PLC0415

    return namespace["_evidence_sc"](_P(ws), files if files is not None else ["answer.md"])


# --- G4 -------------------------------------------------------------------- #




def test_g4_is_idempotent(parent_dir: Path) -> None:
    parent_text = (parent_dir / "tests/verifier/verify.py").read_text(encoding="utf-8")
    once = g4_verify(parent_text)
    assert g4_verify(once) == once


def test_g4_refuses_without_anchor() -> None:
    with pytest.raises(VariantInvalid, match="fallback anchor"):
        g4_verify("def _evidence_sc(ws, files):\n    return ''\n")


def test_g4_only_verify_changes(parent_dir: Path) -> None:
    changes, _ = g4_changes(parent_dir)
    assert set(changes) == {"tests/verifier/verify.py"}



HELPERS_STUB = """from pathlib import Path
def locate_deliverable(ws, *kws):
    for p in sorted(Path(ws).rglob('*')):
        if not p.is_file():
            continue
        stem = p.stem
        if any(k.lower() in stem.lower() for k in kws):
            return p
    return None
"""


def test_g4_missing_answer_stays_missing(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "cicpa_answer_11_report.pdf").write_bytes(b"shipped content")
    parent_text = VERIFY_TEMPLATE
    assert "cicpa_answer_11_report.pdf" in _exec_evidence(parent_text, HELPERS_STUB, ws)  # parent grades the PDF
    patched = g4_verify(parent_text)
    assert "(\u6587\u4ef6\u7f3a\u5931)" in _exec_evidence(patched, HELPERS_STUB, ws)


def test_g4_other_files_keep_fallback(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "deliverable_final_v2.xlsx").write_bytes(b"renamed deliverable")
    patched = g4_verify(VERIFY_TEMPLATE)
    namespace: dict = {}
    helpers_path = tmp_path / "h" / "_helpers.py"
    helpers_path.parent.mkdir(parents=True, exist_ok=True)
    helpers_path.write_text(HELPERS_STUB, encoding="utf-8")
    src = patched.replace('HERE = Path(__file__).resolve().parent',
                          f'HERE = Path({str(helpers_path.parent)!r})')
    exec(compile(src, "<verify.py>", "exec"), namespace)  # noqa: S102
    from pathlib import Path as _P  # noqa: PLC0415

    out = namespace["_evidence_sc"](_P(ws), ["deliverable.xlsx"])
    assert "deliverable_final_v2.xlsx" in out


def test_g4_present_answer_unchanged(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "answer.md").write_text("agent answer", encoding="utf-8")
    patched = g4_verify(VERIFY_TEMPLATE)
    assert "answer.md" in _exec_evidence(patched, HELPERS_STUB, ws)
