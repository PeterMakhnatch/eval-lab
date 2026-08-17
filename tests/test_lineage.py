"""Tests for E14 recursive lineage walker over generated artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evallab.cli import run_cli
from evallab.lineage import (
    classify_zone,
    compute_file_digest,
    lineage_to_dict,
    normalize_digest,
    render_lineage_tree,
    resolve_lineage,
)


def _sha256_text(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def test_classify_zone_boundaries() -> None:
    """Verify zone classification for standard repository layouts."""
    assert classify_zone("runs/job_01/trial_01/result.json") == "z1"
    assert classify_zone("research/evidence/runs/job_01/manifest.json") == "z1"
    assert classify_zone("library/benchmarks/_trajectories/hf/trace.jsonl") == "z1"
    assert classify_zone("derived/parquet/trial_facts/part.parquet") == "z3"
    assert classify_zone("derived/analyses/01J/analysis.json") == "z3"
    assert classify_zone("docs/lineage.md") == "z4"
    assert classify_zone("research/lessons.md") == "z4"
    assert classify_zone("board/open/mission.yaml") == "z5"
    assert classify_zone("agents/roles/builder.yaml") == "z5"
    assert classify_zone("sql/schema.sql") == "z2"


def test_fixture_chain_stops_at_z1(tmp_path: Path) -> None:
    """A Z1 evidence file, a derived artifact citing it, and a second-order artifact.

    Asserts the walk returns all three in order and terminates at Z1.
    """
    # 1. Z1 Evidence file
    z1_dir = tmp_path / "runs" / "job_alpha" / "trial_001"
    z1_dir.mkdir(parents=True)
    z1_file = z1_dir / "result.json"
    z1_content = '{"reward": 1.0, "status": "completed"}'
    z1_file.write_text(z1_content, encoding="utf-8")
    z1_digest = _sha256_text(z1_content)

    # 2. Z3 Derived artifact (JSON sidecar) citing Z1
    derived_dir = tmp_path / "derived" / "analyses"
    derived_dir.mkdir(parents=True)
    derived_file = derived_dir / "analysis.json"
    derived_data = {
        "analysis_id": "01JXYZ",
        "inputs": [
            {
                "path": "runs/job_alpha/trial_001/result.json",
                "digest": z1_digest,
            }
        ],
        "summary": "model succeeded on task",
    }
    derived_content = json.dumps(derived_data, indent=2)
    derived_file.write_text(derived_content, encoding="utf-8")
    derived_digest = _sha256_text(derived_content)

    # 3. Z4 Second-order derived doc (Markdown front-matter) citing Z3
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True)
    doc_file = docs_dir / "finding.md"
    doc_content = f"""---
status: living
audience:
  - builder
inputs:
  - path: derived/analyses/analysis.json
    digest: {derived_digest}
---

# Finding on alpha job
"""
    doc_file.write_text(doc_content, encoding="utf-8")
    doc_digest = _sha256_text(doc_content)

    # Walk lineage
    root_node = resolve_lineage("docs/finding.md", repo_root=tmp_path)

    # Assert root (finding.md)
    assert root_node.target == "docs/finding.md"
    assert root_node.zone == "z4"
    assert root_node.digest == doc_digest
    assert root_node.status == "resolved"
    assert root_node.resolved is True
    assert len(root_node.inputs) == 1

    # Assert middle (analysis.json)
    mid_node = root_node.inputs[0]
    assert mid_node.target == "derived/analyses/analysis.json"
    assert mid_node.zone == "z3"
    assert mid_node.digest == derived_digest
    assert mid_node.status == "resolved"
    assert mid_node.resolved is True
    assert len(mid_node.inputs) == 1

    # Assert leaf (result.json, terminal Z1)
    leaf_node = mid_node.inputs[0]
    assert leaf_node.target == "runs/job_alpha/trial_001/result.json"
    assert leaf_node.zone == "z1"
    assert leaf_node.digest == z1_digest
    assert leaf_node.status == "terminal"
    assert leaf_node.resolved is True
    assert len(leaf_node.inputs) == 0

    # CLI returns 0
    code = run_cli(["lineage", "docs/finding.md"], workspace=tmp_path)
    assert code == 0


def test_digest_mismatch_reported_as_mismatch(tmp_path: Path) -> None:
    """Assert a digest mismatch is reported as a mismatch and fails resolution."""
    z1_dir = tmp_path / "runs" / "job_beta" / "trial_001"
    z1_dir.mkdir(parents=True)
    z1_file = z1_dir / "result.json"
    z1_file.write_text('{"reward": 0.0}', encoding="utf-8")

    bad_digest = "sha256:" + "0" * 64
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True)
    doc_file = docs_dir / "bad_cite.md"
    doc_content = f"""---
status: living
audience:
  - builder
inputs:
  - path: runs/job_beta/trial_001/result.json
    digest: {bad_digest}
---

# Bad cite doc
"""
    doc_file.write_text(doc_content, encoding="utf-8")

    root_node = resolve_lineage("docs/bad_cite.md", repo_root=tmp_path)

    assert root_node.resolved is False
    assert len(root_node.inputs) == 1
    child = root_node.inputs[0]
    assert child.status == "digest_mismatch"
    assert child.resolved is False
    assert child.expected_digest == bad_digest
    assert child.actual_digest is not None
    assert "digest mismatch" in (child.reason or "")

    code = run_cli(["lineage", "docs/bad_cite.md"], workspace=tmp_path)
    assert code != 0


def test_unrecorded_artifact_reported_with_reason(tmp_path: Path) -> None:
    """Assert an artifact without inputs is reported as unrecorded with reason."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True)
    doc_file = docs_dir / "unrecorded.md"
    doc_content = """---
status: living
audience:
  - builder
---

# Unrecorded Doc
No inputs declared here.
"""
    doc_file.write_text(doc_content, encoding="utf-8")

    root_node = resolve_lineage("docs/unrecorded.md", repo_root=tmp_path)

    assert root_node.status == "unrecorded"
    assert root_node.resolved is False
    assert root_node.reason == "no inputs field declared in front-matter"
    assert len(root_node.inputs) == 0

    code = run_cli(["lineage", "docs/unrecorded.md"], workspace=tmp_path)
    assert code != 0


def test_self_citing_artifact_reported_as_cycle(tmp_path: Path) -> None:
    """Assert a self-citing artifact is reported as a cycle and does not loop."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True)
    doc_file = docs_dir / "cycle.md"
    doc_digest = compute_file_digest(doc_file) if doc_file.exists() else "sha256:" + "a" * 64
    doc_content = f"""---
status: living
audience:
  - builder
inputs:
  - path: docs/cycle.md
    digest: {doc_digest}
---

# Self Citing Doc
"""
    doc_file.write_text(doc_content, encoding="utf-8")
    actual_digest = compute_file_digest(doc_file)
    # Update with actual digest
    doc_file.write_text(doc_content.replace(doc_digest, actual_digest), encoding="utf-8")

    root_node = resolve_lineage("docs/cycle.md", repo_root=tmp_path)

    assert root_node.resolved is False
    assert len(root_node.inputs) == 1
    child = root_node.inputs[0]
    assert child.status == "cycle"
    assert child.resolved is False
    assert "cycle detected" in (child.reason or "")

    code = run_cli(["lineage", "docs/cycle.md"], workspace=tmp_path)
    assert code != 0


def test_indirect_cycle_detected(tmp_path: Path) -> None:
    """Assert indirect cycle A -> B -> A is detected."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True)

    a_file = docs_dir / "a.md"
    b_file = docs_dir / "b.md"

    a_file.write_text(
        """---
status: living
audience: [builder]
inputs:
  - path: docs/b.md
---
# A
""",
        encoding="utf-8",
    )

    b_file.write_text(
        """---
status: living
audience: [builder]
inputs:
  - path: docs/a.md
---
# B
""",
        encoding="utf-8",
    )

    root_node = resolve_lineage("docs/a.md", repo_root=tmp_path)
    assert root_node.resolved is False
    assert len(root_node.inputs) == 1
    b_node = root_node.inputs[0]
    assert len(b_node.inputs) == 1
    cycle_node = b_node.inputs[0]
    assert cycle_node.status == "cycle"
    assert "docs/a.md" in (cycle_node.reason or "")


def test_json_output_byte_identical_across_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Assert --json output is byte-identical across two runs."""
    z1_dir = tmp_path / "runs" / "job_det" / "trial_01"
    z1_dir.mkdir(parents=True)
    z1_file = z1_dir / "result.json"
    z1_file.write_text('{"status": "ok"}', encoding="utf-8")
    d1 = compute_file_digest(z1_file)

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True)
    doc_file = docs_dir / "doc.md"
    doc_file.write_text(
        f"""---
status: living
audience: [builder]
inputs:
  - path: runs/job_det/trial_01/result.json
    digest: {d1}
---
# Determinism Test
""",
        encoding="utf-8",
    )

    code1 = run_cli(["lineage", "docs/doc.md", "--json"], workspace=tmp_path)
    out1 = capsys.readouterr().out
    code2 = run_cli(["lineage", "docs/doc.md", "--json"], workspace=tmp_path)
    out2 = capsys.readouterr().out

    assert code1 == 0 and code2 == 0
    assert out1 == out2
    assert len(out1.strip()) > 0
    parsed = json.loads(out1)
    assert parsed["resolved"] is True
    assert parsed["status"] == "resolved"
    assert parsed["zone"] == "z4"


def test_nonexistent_target_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Assert a nonexistent target exits non-zero."""
    code = run_cli(["lineage", "nonexistent/file.md"], workspace=tmp_path)
    out = capsys.readouterr().out
    assert code != 0
    assert "not_found" in out

    # Also test --json mode
    code_json = run_cli(["lineage", "nonexistent/file.md", "--json"], workspace=tmp_path)
    out_json = capsys.readouterr().out
    assert code_json != 0
    payload = json.loads(out_json)
    assert payload["status"] == "not_found"
    assert payload["resolved"] is False


def test_missing_input_file_reported(tmp_path: Path) -> None:
    """Assert a missing input file is reported with not_found status."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True)
    doc_file = docs_dir / "missing_cite.md"
    doc_file.write_text(
        """---
status: living
audience: [builder]
inputs:
  - path: runs/ghost_job/trial_01/result.json
    digest: sha256:1111111111111111111111111111111111111111111111111111111111111111
---
# Ghost Cite
""",
        encoding="utf-8",
    )

    root_node = resolve_lineage("docs/missing_cite.md", repo_root=tmp_path)
    assert root_node.resolved is False
    assert len(root_node.inputs) == 1
    assert root_node.inputs[0].status == "not_found"
    assert "not found" in (root_node.inputs[0].reason or "")


def test_render_lineage_tree_formatting(tmp_path: Path) -> None:
    """Verify ASCII tree rendering structure."""
    z1_dir = tmp_path / "runs" / "job_tree" / "trial_01"
    z1_dir.mkdir(parents=True)
    z1_file = z1_dir / "result.json"
    z1_file.write_text('{"v": 1}', encoding="utf-8")
    d1 = compute_file_digest(z1_file)

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True)
    doc_file = docs_dir / "tree.md"
    doc_file.write_text(
        f"""---
status: living
audience: [builder]
inputs:
  - path: runs/job_tree/trial_01/result.json
    digest: {d1}
---
# Tree
""",
        encoding="utf-8",
    )

    root = resolve_lineage("docs/tree.md", repo_root=tmp_path)
    tree_text = render_lineage_tree(root)

    assert "docs/tree.md [z4]" in tree_text
    assert "└── runs/job_tree/trial_01/result.json [z1]" in tree_text
    assert "[terminal]" in tree_text
    assert d1 in tree_text



def test_lineage_to_dict_structure(tmp_path: Path) -> None:
    """Verify dictionary serialization for lineage nodes."""
    z1_dir = tmp_path / "runs" / "job_dict" / "trial_01"
    z1_dir.mkdir(parents=True)
    z1_file = z1_dir / "result.json"
    z1_file.write_text('{"v": 2}', encoding="utf-8")
    d1 = compute_file_digest(z1_file)

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True)
    doc_file = docs_dir / "dict_test.md"
    doc_file.write_text(
        f"""---
status: living
audience: [builder]
inputs:
  - path: runs/job_dict/trial_01/result.json
    digest: {d1}
---
# Dict Test
""",
        encoding="utf-8",
    )

    root = resolve_lineage("docs/dict_test.md", repo_root=tmp_path)
    d = lineage_to_dict(root)
    assert d["target"] == "docs/dict_test.md"
    assert d["status"] == "resolved"
    assert d["resolved"] is True
    assert len(d["inputs"]) == 1
    assert d["inputs"][0]["status"] == "terminal"
    assert d["inputs"][0]["zone"] == "z1"
def test_normalize_digest_helper() -> None:
    """Verify normalize_digest handles bare hex and sha256: prefixes."""
    assert normalize_digest(None) is None
    bare_hex = "a" * 64
    assert normalize_digest(bare_hex) == f"sha256:{bare_hex}"
    prefixed = f"sha256:{'B' * 64}"
    assert normalize_digest(prefixed) == f"sha256:{'b' * 64}"
    custom = "md5:123"
    assert normalize_digest(custom) == "md5:123"
