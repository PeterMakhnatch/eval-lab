"""Behavioral edges for non-gating selection evidence, not the test gate."""

from __future__ import annotations

import importlib.util
import json
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from evallab.selection_manifest import build_manifest, environment_rules, summary

ROOT = Path(__file__).resolve().parents[1]


def commit(root: Path) -> str:
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "commit", "-m", "fixture"],
                   check=True, capture_output=True)
    return subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()


def test_base_selector_reads_head_without_executing_it(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    (root / "src/evallab").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    (root / "src/evallab/devloop.py").write_bytes((ROOT / "src/evallab/devloop.py").read_bytes())
    (root / "src/evallab/old.py").write_text("VALUE = 1\n")
    (root / "tests/test_consumer.py").write_text("from evallab.old import VALUE\n")
    (root / "tests/test_other.py").write_text("def test_other(): pass\n")
    base = commit(root)
    (root / "src/evallab/old.py").rename(root / "src/evallab/new.py")
    (root / "src/evallab/devloop.py").write_text("raise AssertionError('PR selector executes')\n")
    head = commit(root)
    # Working-tree edits must not affect the immutable input.
    (root / "tests/test_other.py").write_text("from evallab.old import VALUE\n")
    manifest = build_manifest(root, head, base)
    checkpoint = manifest["policies"]["checkpoint"]
    assert checkpoint["selected_modules"] == ["tests/test_consumer.py"]
    assert checkpoint["reasons"]["tests/test_consumer.py"]
    assert {"src/evallab/old.py", "src/evallab/new.py"} <= set(manifest["changed_paths"])
    assert manifest["selector_revision"] == base
    assert manifest["merge_base_sha"] == base
    environment = manifest["policies"]["checkpoint+environment"]
    assert environment["fallback"]
    assert environment["selected_modules"] == manifest["inventory"]
    assert {item["rule"] for item in environment["matched_rules"]} == {
        "environment:test-execution-or-selector"
    }
    assert len(summary(manifest).splitlines()) == 5
    (root / "tests/test_other.py").write_text("def test_other(): pass\n")
    subprocess.run(["git", "-C", str(root), "checkout", "-b", "base-side", base], check=True, capture_output=True)
    (root / "tests/test_added_on_base.py").write_text("def test_added(): pass\n")
    newer_base = commit(root)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c",
                    "user.email=test@example.invalid", "merge", "--no-ff", head, "-m", "merge fixture"],
                   check=True, capture_output=True)
    tested = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    merged = build_manifest(root, head, newer_base, tested)
    assert "tests/test_added_on_base.py" in merged["policies"]["checkpoint+environment"]["selected_modules"]
    assert merged["tested_sha"] == tested and merged["base_sha"] == newer_base
    (root / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["."]\n')
    invalid = commit(root)
    with pytest.raises(ValueError, match="invalid testpaths"):
        build_manifest(root, invalid, base)


def test_environment_rule_one_boundaries() -> None:
    for path in (
        "nested/conftest.py", "uv.lock", "tools/worker/Pipfile", "setup.py",
        ".github/workflows/ci.yml", "Makefile", "scripts/pytest-shard.py",
        "scripts/ci/check_harbor_skips.py", "nested/sitecustomize.py",
        "src/evallab/selection_manifest.py",
    ):
        assert environment_rules(path), path
    for path in ("src/evallab/runner.py", "docs/ship.md", "tests/test_runner.py"):
        assert environment_rules(path) == [], path
    message = summary({"status": "error", "head_sha": "head", "base_sha": "base", "error": "bad history"})
    assert len(message.splitlines()) == 5
    assert "::error::" in message and "bad history" in message


def analyzer():
    spec = importlib.util.spec_from_file_location("shadow_analyzer", ROOT / "scripts/analyze-selection-shadow.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def observation(run_id: int, failed: bool = False) -> dict:
    receipts = [
        {"shard": f"{shard}/4", "phase": phase, "exitstatus": int(failed),
         "collection_errors": [], "failed_modules": ["tests/test_missed.py"] if failed else [],
         "modules": {"tests/test_selected.py": {"seconds": 1.0},
                     "tests/test_missed.py": {"seconds": 3.0}}}
        for shard in range(1, 5) for phase in ("full-suite", "harbor-native")
    ]
    return {"run_id": run_id, "pr": 12, "created_at": str(run_id), "receipts": receipts,
            "manifest": {"head_sha": "head", "base_sha": "base", "selector_sha256": "selector",
                         "inventory": ["tests/test_selected.py", "tests/test_missed.py"], "policies": {
                "checkpoint": {"selected_modules": ["tests/test_selected.py"], "fallback": False},
                "checkpoint+environment": {"selected_modules": ["tests/test_selected.py", "tests/test_missed.py"],
                                           "fallback": True}}}}


def test_analyzer_deduplicates_failures_and_prices_complete_sessions() -> None:
    module = analyzer()
    result = module.analyze([observation(1, failed=True), observation(2, failed=True), observation(3)])
    raw = result["policies"]["checkpoint"]
    assert raw["failed_head_module_pairs"] == 1
    assert raw["module_recall"] == 0
    assert raw["prs_with_omitted_failure"] == [12]
    assert raw["selected_test_seconds_share"] == {"n": 1, "mean": 0.25, "median": 0.25, "p90": 0.25}
    fallback = result["policies"]["checkpoint+environment"]
    assert fallback["module_recall"] == 1
    assert fallback["selected_test_seconds_share"]["median"] == 1
    assert fallback["fallback_rate"] == 1


def test_analyzer_never_prices_missing_or_failed_sessions_as_zero() -> None:
    module = analyzer()
    missing = observation(1)
    missing["receipts"].pop()
    result = module.analyze([missing])["policies"]["checkpoint"]
    assert result["selected_test_seconds_share"]["median"] is None
    assert result["unpriced_runs"] == [1]
    assert result["module_recall"] is None
    assert result["escape_proxy"] is None
    assert result["escape_proxy_lower_bound"] == 0
    assert result["escape_proxy_upper_bound"] == 1
    failed = module.analyze([observation(2, failed=True)])["policies"]["checkpoint"]
    assert failed["selected_test_seconds_share"]["n"] == 0
    duplicate = observation(3, failed=True)
    duplicate["manifest"]["policies"]["checkpoint"]["selected_modules"].append("tests/test_missed.py")
    with pytest.raises(ValueError, match="selection differs"):
        module.analyze([observation(1, failed=True), duplicate])
    duplicate["manifest"]["base_sha"] = "retargeted-base"
    retargeted = module.analyze([observation(1, failed=True), duplicate])
    assert retargeted["policies"]["checkpoint"]["failed_head_module_pairs"] == 2
    crash = observation(4)
    crash["receipts"][0]["exitstatus"] = 3
    crash["receipts"][1]["collection_errors"] = ["tests/conftest.py", ""]
    result = module.analyze([crash])
    assert "unattributed exit 3" in result["unobserved_runs"][0]["issues"][0]
    policy = result["policies"]["checkpoint+environment"]
    assert policy["module_recall"] is None
    assert len(policy["collection_errors_without_module_attribution"]) == 2
    collection = observation(5)
    collection["receipts"][0]["exitstatus"] = 2
    collection["receipts"][0]["collection_errors"] = ["tests/conftest.py"]
    result = module.analyze([collection])
    assert result["policies"]["checkpoint"]["escape_proxy"] is None
    assert result["policies"]["checkpoint"]["escape_proxy_upper_bound"] == 1


def test_analyzer_reports_manifest_errors_and_rejects_wrong_selector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = analyzer()
    metadata = {"event": "pull_request", "name": "quality", "status": "completed"}
    monkeypatch.setattr(module, "gh", lambda *args: json.dumps(metadata))
    path = tmp_path / "selection-manifest.json"
    path.write_text(json.dumps({"schema_version": 1, "status": "error", "error": "base unavailable"}))
    with pytest.raises(ValueError, match="manifest error: base unavailable"):
        module.load_run("owner/repo", 123, tmp_path)
    manifest = {"schema_version": 1, "status": "ok", "tested_sha": "a" * 40,
                "base_sha": "b" * 40, "merge_base_sha": "c" * 40,
                "selector_revision": "d" * 40, "selector_sha256": "e" * 64}
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="selector revision differs"):
        module.load_run("owner/repo", 123, tmp_path)


def test_duration_receipt_includes_fixture_time_and_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = runpy.run_path(str(ROOT / "tests/conftest.py"))["_ModuleDurations"]()
    for phase, duration in (("setup", 2.0), ("call", 0.1), ("teardown", 3.0)):
        plugin.pytest_runtest_logreport(SimpleNamespace(
            nodeid="tests/test_case.py::test_case", duration=duration, when=phase, failed=phase == "teardown"
        ))
    plugin.pytest_collectreport(SimpleNamespace(failed=True, nodeid="tests/test_import.py"))
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    xmlpath = tmp_path / "new-directory/full-suite.xml"
    config = SimpleNamespace(getoption=lambda name, default=None: {"xmlpath": str(xmlpath), "shard": "1/4"}.get(name, default))
    plugin.pytest_sessionfinish(SimpleNamespace(config=config, testscollected=1), 1)
    receipt = json.loads(xmlpath.with_suffix(".modules.json").read_text())
    assert receipt["modules"] == {"tests/test_case.py": {"seconds": 5.1, "tests": 1}}
    assert receipt["failed_modules"] == ["tests/test_case.py"]
    assert receipt["collection_errors"] == ["tests/test_import.py"]
    assert receipt["run_id"] == "123" and receipt["shard"] == "1/4"


def test_manifest_command_rejects_pr_stdlib_shadowing(tmp_path: Path) -> None:
    script = tmp_path / "selection_manifest.py"
    script.write_bytes((ROOT / "src/evallab/selection_manifest.py").read_bytes())
    (tmp_path / "argparse.py").write_text("raise AssertionError('PR stdlib shadow executes')\n")
    unsafe = subprocess.run([sys.executable, str(script), "--help"], text=True, capture_output=True)
    assert unsafe.returncode != 0
    assert "requires python -I" in unsafe.stderr
    safe = subprocess.run([sys.executable, "-I", str(script), "--help"], text=True, capture_output=True)
    assert safe.returncode == 0
    assert "--tested" in safe.stdout
