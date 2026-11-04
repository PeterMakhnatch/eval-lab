"""Strict per-corpus acceptance, cohort identity, and approval boundaries."""

from __future__ import annotations

import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("line_gate", ROOT / "scripts/line-gate.py")
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def board(cheats=(2, 3, 5), honest=(7, 2, 1), name="a"):
    ck, cr, cd = cheats
    hk, hr, hd = honest
    return {
        "by_corpus": {
            name: {
                "cheats": ck + cr + cd,
                "clean": hk + hr + hd,
                "cohort_hash": name,
                "labels_hash": "frozen",
                "labelled_runs": sum(cheats + honest),
                "metrics": {
                    "drop_recall": {"k": cd},
                    "drop_or_review_recall": {"k": cd + cr},
                    "clean_false_alarm_rate": {"k": hd},
                    "clean_drop_or_review_rate": {"k": hd + hr},
                },
            }
        }
    }


def test_per_corpus_cheat_keep_increase_blocks_even_when_pooled_improves():
    before = board()
    before["by_corpus"].update(board(name="b")["by_corpus"])
    after = board(cheats=(3, 2, 5))
    after["by_corpus"].update(board(cheats=(0, 5, 5), name="b")["by_corpus"])
    rows, blockers = gate.compare(before, after)
    assert blockers == [{"corpus": "a", "kind": "cheat_keep_increase", "delta": 1}]
    assert rows[0]["base"] == {"keep": 2, "review": 3, "drop": 5}
    assert rows[0]["head"] == {"keep": 3, "review": 2, "drop": 5}
    assert rows[0]["delta"] == {"keep": 1, "review": -1, "drop": 0}


@pytest.mark.parametrize("honest,blocked", [((6, 2, 2), False), ((5, 2, 3), True)])
def test_existing_honest_drop_allowance(honest, blocked):
    _, blockers = gate.compare(board(), board(honest=honest))
    assert bool(blockers) is blocked


def test_honest_drop_percentage_boundary():
    _, blockers = gate.compare(board(honest=(198, 0, 2)), board(honest=(196, 0, 4)))
    assert blockers == []
    _, blockers = gate.compare(board(honest=(198, 0, 2)), board(honest=(195, 0, 5)))
    assert blockers == [{"corpus": "a", "kind": "honest_drop_increase", "delta": 3}]


@pytest.mark.parametrize("field", ["cohort_hash", "labels_hash", "labelled_runs"])
def test_identity_changes_are_errors(field):
    before = board()
    after = deepcopy(before)
    after["by_corpus"]["a"][field] = "changed"
    with pytest.raises(ValueError, match="unmatched DEV"):
        gate.compare(before, after)


def test_corpus_removal_is_error():
    with pytest.raises(ValueError, match="mismatch"):
        gate.compare(board(), board(name="b"))


def test_approval_binds_pr_head_and_every_corpus_delta():
    rows, _ = gate.compare(board(), board(cheats=(3, 2, 5)))
    deltas = [
        {"corpus": r["corpus"], "class": r["class"], "delta": r["delta"]}
        for r in rows
        if any(r["delta"].values())
    ]
    entry = {
        "pr": 123,
        "head": "a" * 40,
        "approved_by": "Peter",
        "approval_quote": "Approve this exact DEV change.",
        "corpus_deltas": deltas,
    }
    policy = yaml.safe_dump({"line_gate_approvals": [entry]})
    assert gate.approval(policy, 123, "a" * 40, rows) == entry
    assert gate.approval(policy, 124, "a" * 40, rows) is None
    assert gate.approval(policy, None, "a" * 40, rows) is None
    assert gate.approval(policy, 123, "b" * 40, rows) is None
    changed = deepcopy(rows)
    changed[0]["delta"]["keep"] = 2
    assert gate.approval(policy, 123, "a" * 40, changed) is None
    entry["approved_by"] = "agent"
    assert (
        gate.approval(yaml.safe_dump({"line_gate_approvals": [entry]}), 123, "a" * 40, rows) is None
    )


def test_import_closure_covers_relative_conditional_and_package_imports(tmp_path):
    files = {
        "scripts/line-replay.py": "from evallab.line.replay import replay\n",
        "src/evallab/__init__.py": "",
        "src/evallab/line/__init__.py": "",
        "src/evallab/line/replay.py": "from .core import replay\n",
        "src/evallab/line/core.py": "if False:\n    from evallab import detector\n",
        "src/evallab/detector.py": "RULE = 1\n",
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    paths = gate.line_paths(tmp_path)
    assert set(files) <= set(paths)


def test_missing_data_never_passes(tmp_path, monkeypatch):
    import subprocess

    root = tmp_path / "repo"
    root.mkdir()
    for name in ("scripts/line-replay.py", "src/evallab/__init__.py", "pyproject.toml", "uv.lock"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    for args in (
        ["init", "--quiet"],
        ["add", "scripts/line-replay.py", "src/evallab/__init__.py", "pyproject.toml", "uv.lock"],
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ],
        ["update-ref", "refs/remotes/origin/main", "HEAD"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    monkeypatch.setattr(gate, "ROOT", root)
    output = tmp_path / "receipt"
    assert gate.main(["--set", str(tmp_path / "absent"), "--output", str(output)]) == 2
    import json

    report = json.loads((output / "gate.json").read_text())
    assert report["passed"] is False
    assert report["verdict"] == "ERROR"
    assert report["error"] == "line gate: replay data unavailable, run locally"


def test_resource_discovery_uses_committed_tree_inventory(tmp_path):
    entry = tmp_path / "scripts/line-replay.py"
    entry.parent.mkdir()
    entry.write_text('RULES = "policy/rules.json"\n')
    # Resource bytes need not be extracted or imported during path discovery.
    assert "policy/rules.json" in gate.line_paths(tmp_path, {"policy/rules.json"})


def test_existing_output_is_error_and_preserves_receipt(tmp_path):
    receipt = tmp_path / "gate.json"
    receipt.write_text("preserve existing evidence")
    assert gate.main(["--output", str(tmp_path)]) == 2
    assert receipt.read_text() == "preserve existing evidence"
