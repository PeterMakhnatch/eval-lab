from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from evallab.program_report import build_report, index_facts, publish_bridge


def program(tmp_path: Path, text: str) -> Path:
    lane = tmp_path / "program" / "alpha"
    lane.mkdir(parents=True)
    (lane / "RESULT.md").write_text(text)
    return lane.parent


@pytest.fixture(autouse=True)
def no_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("evallab.program_report.verify_lanes", lambda *_: {
        "availability": {"status": "unchecked", "reason": "absent"}})


def test_unreceipted_number_blocks_draft_with_verbatim_location(tmp_path: Path) -> None:
    source = program(tmp_path, "# Result\nWe accept 17 runs.\n")
    output = tmp_path / "output"
    with pytest.raises(ValueError, match=r"RESULT.md:2.*missing_receipt: We accept 17 runs"):
        build_report(source, output, tmp_path)
    assert not (output / "REPORT.md").exists()
    assert json.loads((output / "rejected.json").read_text())[0]["quote"] == "We accept 17 runs."


def test_receipt_cannot_supply_an_unrelated_number(tmp_path: Path) -> None:
    source = program(tmp_path, "We accept 17 runs (`receipt.json`).\n")
    (source / "alpha" / "receipt.json").write_text('{"runs": 12}')
    with pytest.raises(ValueError, match="unsupported_numbers"):
        build_report(source, tmp_path / "out", tmp_path)

@pytest.mark.parametrize(("rate", "passed"), [("66.7", True), ("70.0", False)])
def test_ratio_recomputed_even_when_receipt_repeats_wrong_rate(
    tmp_path: Path, rate: str, passed: bool,
) -> None:
    statement = f"Pass rate: 2/3 = {rate}% (`receipt.md`).\n"
    source = program(tmp_path, statement)
    (source / "alpha" / "receipt.md").write_text(statement)
    output = tmp_path / "out"
    if passed:
        build_report(source, output, tmp_path)
    else:
        with pytest.raises(ValueError, match="arithmetic_mismatch"):
            build_report(source, output, tmp_path)
    check = next(iter(json.loads((output / "checks.json").read_text()).values()))
    assert check["arithmetic"][0]["recomputed"] == 2 / 3
    assert check["arithmetic"][0]["passed"] is passed



def test_full_index_preserves_unselected_claims_and_quote_guard(tmp_path: Path) -> None:
    source = program(tmp_path, "We accept 17 runs.\nWe accept 99 runs.\n")
    receipt = tmp_path / "receipt.json"
    receipt.write_text('{"runs": 17}')
    facts = index_facts(source, tmp_path)
    citations = tmp_path / "citations.json"
    citations.write_text(json.dumps([{"id": facts[0].id, "quote": facts[0].quote,
                                     "receipts": [str(receipt)]}]))
    output = build_report(source, tmp_path / "out", tmp_path,
                          select=[facts[0].id], citations=citations)
    draft = (output / "REPORT.md").read_text()
    assert f"We accept 17 runs. | [{facts[0].id}]" in draft
    assert "99 runs" not in draft
    indexed = json.loads((output / "facts.json").read_text())
    assert indexed[1]["quote"] == "We accept 99 runs."
    assert indexed[0]["status"] == "receipt_bound_unchecked"
    citations.write_text(json.dumps([{"id": facts[0].id, "quote": "Changed", "receipts": []}]))
    with pytest.raises(ValueError, match="quote changed"):
        build_report(source, tmp_path / "other", tmp_path, citations=citations)


def test_bridge_commits_only_report_path_and_pushes(tmp_path: Path) -> None:
    repo = tmp_path / "research-context"
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    for key, value in [("user.email", "test@example.org"), ("user.name", "Test")]:
        subprocess.run(["git", "config", key, value], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
    (repo / "base").write_text("base")
    subprocess.run(["git", "add", "base"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    (repo / "other-writer").write_text("preserve")
    subprocess.run(["git", "add", "other-writer"], cwd=repo, check=True)
    output = tmp_path / "output"
    output.mkdir()
    (output / "REPORT.md").write_text("Report")
    publish_bridge(output, repo / "bridge" / "report")
    changed = subprocess.run(["git", "show", "--format=", "--name-only", "HEAD"],
                             cwd=repo, check=True, capture_output=True, text=True).stdout.splitlines()
    assert changed == ["bridge/report/REPORT.md"]
    assert (repo / "other-writer").read_text() == "preserve"
    pushed = subprocess.run(["git", "rev-parse", "origin/main"], cwd=repo,
                            check=True, capture_output=True, text=True).stdout
    local = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                           check=True, capture_output=True, text=True).stdout
    assert pushed == local
    with pytest.raises(FileExistsError):
        publish_bridge(output, repo / "bridge" / "report")
