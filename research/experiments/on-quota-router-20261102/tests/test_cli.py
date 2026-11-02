"""Tests for quota_router CLI interface."""

from __future__ import annotations

import json
from pathlib import Path

from quota_router.__main__ import main

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def test_cli_pick_success(capsys) -> None:
    usage_file = FIXTURES_DIR / "omp_usage_real_scrubbed.txt"
    exit_code = main([
        "pick",
        "google-antigravity/gemini-3.8-flash",
        "--hours", "1.0",
        "--usage-file", str(usage_file),
    ])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "google-antigravity/gemini-3.8-flash" in captured.out


def test_cli_pick_refuse(capsys) -> None:
    usage_file = FIXTURES_DIR / "omp_usage_above_gate.txt"
    exit_code = main([
        "pick",
        "anthropic/claude-sonnet-5-5",
        "--hours", "1.0",
        "--usage-file", str(usage_file),
    ])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "REFUSE:" in captured.out


def test_cli_pick_json(capsys) -> None:
    usage_file = FIXTURES_DIR / "omp_usage_real_scrubbed.txt"
    exit_code = main([
        "pick",
        "google-antigravity/gemini-3.8-flash",
        "--hours", "1.0",
        "--usage-file", str(usage_file),
        "--json",
    ])
    captured = capsys.readouterr()
    assert exit_code == 0
    data = json.loads(captured.out)
    assert data["route"] == "google-antigravity/gemini-3.8-flash"
    assert data["refused"] is False


def test_cli_check(capsys) -> None:
    usage_file = FIXTURES_DIR / "omp_usage_real_scrubbed.txt"
    exit_code = main([
        "check",
        "google-antigravity/gemini-3.8-flash",
        "--hours", "1.0",
        "--usage-file", str(usage_file),
    ])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "OK:" in captured.out


def test_cli_parse(capsys) -> None:
    usage_file = FIXTURES_DIR / "omp_usage_real_scrubbed.txt"
    exit_code = main([
        "parse",
        "--usage-file", str(usage_file),
        "--json",
    ])
    captured = capsys.readouterr()
    assert exit_code == 0
    data = json.loads(captured.out)
    assert "claude_7d" in data["metrics"]
    assert data["metrics"]["claude_7d"]["percent_used"] == 7.0


def test_cli_scrub(tmp_path: Path) -> None:
    input_file = tmp_path / "raw.txt"
    input_file.write_text("● alice@corp.com · Peter Makhnatch\n  ● Claude 7 Day  5.0% used\n")
    out_file = tmp_path / "scrubbed.txt"

    exit_code = main([
        "scrub",
        "--usage-file", str(input_file),
        "-o", str(out_file),
    ])
    assert exit_code == 0
    scrubbed = out_file.read_text()
    assert "alice@corp.com" not in scrubbed
    assert "Peter Makhnatch" not in scrubbed
    assert "user@example.com" in scrubbed
