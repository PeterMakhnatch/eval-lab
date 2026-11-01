"""The v2.4 cut: reproducible artifact digest and a clean release_check."""

import json
from pathlib import Path

import pytest
from releasecheck.cli import main, release_check
from releasecheck.collect import collect_artifacts, release_digest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "release/v2.4/manifest.json").read_text())


@pytest.mark.parametrize("delays", [[70, 55, 40, 25, 5], [5, 25, 40, 55, 70], [40, 5, 70, 25, 55]])
def test_digest_does_not_depend_on_completion_order(delays):
    artifacts = [dict(a, fetch_ms=d) for a, d in zip(MANIFEST["artifacts"], delays, strict=True)]
    assert release_digest(collect_artifacts(artifacts)) == MANIFEST["expected_release_digest"]


def test_release_check_reports_every_invariant():
    names = [r.name for r in release_check(ROOT / "release/v2.4")]
    assert names == ["version_monotonic", "changelog_entry", "artifact_digest", "security_audit_gate"]


def test_release_check_is_clean_for_v2_4(capsys):
    assert main([str(ROOT / "release/v2.4")]) == 0
    assert "FAIL" not in capsys.readouterr().out
