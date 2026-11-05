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


POLICY = {
    "daily_cost_ceiling_usd": 20,
    "per_job_cost_ceiling_usd": 3,
    "quiet_failure_rule": 3,
    "auto_run": [{"name": "local-controls", "agents": ["oracle", "nop"]}],
}


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
    policy_data = {**POLICY, "line_gate_approvals": [entry]}
    policy = yaml.safe_dump(policy_data)
    assert gate.approval(policy, 123, "a" * 40, rows) == entry
    assert gate.approval(policy, 124, "a" * 40, rows) is None
    assert gate.approval(policy, None, "a" * 40, rows) is None
    assert gate.approval(policy, 123, "b" * 40, rows) is None
    changed = deepcopy(rows)
    changed[0]["delta"]["keep"] = 2
    assert gate.approval(policy, 123, "a" * 40, changed) is None
    original = gate.StandingApprovalsPolicy.model_validate(POLICY)
    approved = gate.StandingApprovalsPolicy.model_validate(policy_data)
    assert "line_gate_approvals" not in original.model_dump()
    # Recording an exception remains loadable by queue policy consumers.
    restored = gate.StandingApprovalsPolicy.model_validate(approved.model_dump())
    assert restored.line_gate_approvals[0].pr == 123
    assert restored.daily_cost_ceiling_usd == original.daily_cost_ceiling_usd
    assert restored.auto_run == original.auto_run
    entry["approved_by"] = "agent"
    with pytest.raises(ValueError, match="approved_by"):
        gate.approval(yaml.safe_dump(policy_data), 123, "a" * 40, rows)


def test_scope_uses_tracked_paths_without_importing_or_parsing_code(repository):
    root, run = repository
    files = {
        "src/evallab/line/core.py": "invalid Python: discovery must not parse this\n",
        "src/evallab/hack_drop.py": "from evallab import cli\n",
        "src/evallab/cli.py": "from evallab import unrelated\n",
        "src/evallab/unrelated.py": "",
        "research/experiments/unrelated/results.json": "{}\n",
        "docs/line.md": "documentation only\n",
    }
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    run("add", *files)
    untracked = root / "src/evallab/line/untracked.py"
    untracked.write_text("")
    assert gate.line_paths(root) == sorted(
        {
            "scripts/line-replay.py",
            "pyproject.toml",
            "uv.lock",
            "src/evallab/line/core.py",
            "src/evallab/hack_drop.py",
        }
    )


@pytest.fixture
def repository(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "repo"
    root.mkdir()

    def run(*args):
        return subprocess.check_output(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                *args,
            ],
            cwd=root,
            text=True,
        ).strip()

    run("init", "--quiet")
    for name in ("scripts/line-replay.py", "src/evallab/__init__.py", "pyproject.toml", "uv.lock"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    run("add", "scripts/line-replay.py", "src/evallab/__init__.py", "pyproject.toml", "uv.lock")
    run("commit", "--quiet", "-m", "fixture")
    run("update-ref", "refs/remotes/origin/main", "HEAD")
    monkeypatch.setattr(gate, "ROOT", root)
    return root, run


def test_missing_data_never_passes(tmp_path, repository):
    output = tmp_path / "receipt"
    assert gate.main(["--set", str(tmp_path / "absent"), "--output", str(output)]) == 2
    import json

    report = json.loads((output / "gate.json").read_text())
    assert report["passed"] is False
    assert report["verdict"] == "ERROR"
    assert report["error"] == "line gate: replay data unavailable, run locally"


def test_supplied_inventory_includes_paths_absent_from_working_tree(repository):
    root, _ = repository
    inventory = {
        "src/evallab/line/new/nested.py",
        "src/evallab/tool_calls.py",
        "research/answer-keys/splits/assignments.csv",
        "policy/standing-approvals.yaml",
        "docs/line.md",
        "src/evallab/cli.py",
    }
    assert set(gate.line_paths(root, inventory)) & inventory == inventory - {
        "docs/line.md",
        "src/evallab/cli.py",
    }


def test_existing_output_is_error_and_preserves_receipt(tmp_path):
    receipt = tmp_path / "gate.json"
    receipt.write_text("preserve existing evidence")
    assert gate.main(["--output", str(tmp_path)]) == 2
    assert receipt.read_text() == "preserve existing evidence"


def test_origin_snapshot_stays_fixed_when_another_writer_fetches(tmp_path, repository, monkeypatch):
    import json

    root, run = repository
    original = run("rev-parse", "origin/main")
    run("commit", "--allow-empty", "--quiet", "-m", "new base")
    newer = run("rev-parse", "HEAD")
    run("commit", "--allow-empty", "--quiet", "-m", "candidate")
    real_git = gate.git

    def concurrent_fetch(root, *args):
        value = real_git(root, *args)
        if args == ("rev-parse", "--verify", "origin/main^{commit}"):
            run("update-ref", "refs/remotes/origin/main", newer)
        return value

    monkeypatch.setattr(gate, "git", concurrent_fetch)
    output = tmp_path / "snapshot"
    assert gate.main(["--paths", "--output", str(output)]) == 0
    report = json.loads((output / "gate.json").read_text())
    assert report["origin_main"] == original
    assert report["base"] == original


# Exact changed-path inventories from gh pr view <number> --json files.
# Expected paths are production decision changes or gate controls, not docs,
# regression tests or research reports that happen to accompany those changes.
@pytest.mark.parametrize(
    "changed,expected",
    [
        pytest.param(
            {
                "docs/INDEX.md",
                "docs/hack-drop.md",
                "docs/repo-map.md",
                "src/evallab/hack_drop.py",
                "src/evallab/line/replay_sets.json",
                "src/evallab/tool_calls.py",
                "tests/test_hack_drop.py",
                "tests/test_tool_calls.py",
            },
            {
                "src/evallab/hack_drop.py",
                "src/evallab/line/replay_sets.json",
                "src/evallab/tool_calls.py",
            },
            id="pr-1825-tool-call-normalizer",
        ),
        pytest.param(
            {
                "docs/INDEX.md",
                "docs/line.md",
                "src/evallab/line/core.py",
                "src/evallab/line/replay_sets.json",
                "tests/line/test_task_policy.py",
            },
            {
                "src/evallab/line/core.py",
                "src/evallab/line/replay_sets.json",
            },
            id="pr-1887-task-policy",
        ),
        pytest.param(
            {
                "docs/INDEX.md",
                "docs/line.md",
                "docs/repo-map.md",
                "src/evallab/hack_drop.py",
                "src/evallab/line/replay_sets.json",
                "tests/line/test_residuals.py",
                "tests/test_hack_drop.py",
            },
            {
                "src/evallab/hack_drop.py",
                "src/evallab/line/replay_sets.json",
            },
            id="pr-1890-residual-detector",
        ),
        pytest.param(
            {
                "Makefile",
                "docs/INDEX.md",
                "docs/line.md",
                "docs/repo-map.md",
                "research/experiments/ov1106-outcome/README.md",
                "research/experiments/ov1106-outcome/audit_native_copy.py",
                "research/experiments/ov1106-outcome/copy_dev.py",
                "research/experiments/ov1106-outcome/coverage-provenance.json",
                "research/experiments/ov1106-outcome/coverage-recovery.json",
                "research/experiments/ov1106-outcome/coverage-rows.jsonl",
                "research/experiments/ov1106-outcome/coverage-summary.json",
                "research/experiments/ov1106-outcome/coverage.py",
                "research/experiments/ov1106-outcome/coverage_recovery.py",
                "research/experiments/ov1106-outcome/helper-attestation.json",
                "research/experiments/ov1106-outcome/native-copy.json",
                "research/experiments/ov1106-outcome/paired_analysis.py",
                "research/experiments/ov1106-outcome/recovery-search.json",
                "research/experiments/ov1106-outcome/recovery_search.py",
                "research/experiments/ov1106-outcome/replay_table.py",
                "research/experiments/ov1106-outcome/results/line-replay.json",
                "research/experiments/ov1106-outcome/results/line-replay.md",
                "research/experiments/ov1106-outcome/results/paired_metrics.json",
                "research/experiments/ov1106-outcome/results/paired_metrics.md",
                "research/experiments/ov1106-outcome/results/paired_rows.jsonl",
                "scripts/line-replay.py",
                "src/evallab/copy_check.py",
                "src/evallab/hack_drop.py",
                "src/evallab/line/cli.py",
                "src/evallab/line/copy_history.py",
                "src/evallab/line/core.py",
                "src/evallab/line/replay.py",
                "src/evallab/line/spec.py",
                "src/evallab/rollout_admission.py",
                "tests/line/test_copy_history.py",
                "tests/test_rollout_admission.py",
            },
            {
                "Makefile",
                "scripts/line-replay.py",
                "src/evallab/copy_check.py",
                "src/evallab/hack_drop.py",
                "src/evallab/line/cli.py",
                "src/evallab/line/copy_history.py",
                "src/evallab/line/core.py",
                "src/evallab/line/replay.py",
                "src/evallab/line/spec.py",
                "src/evallab/rollout_admission.py",
            },
            id="pr-1900-copy-history",
        ),
    ],
)
def test_historical_prs_trigger_every_decision_path_but_not_reports(
    repository, changed, expected
):
    root, _ = repository
    assert set(gate.line_paths(root, changed)) & changed == expected


@pytest.mark.parametrize(
    "path",
    [
        "src/evallab/production/monitor_worker.py",
        "src/evallab/autoreview_monitor.py",
        "src/evallab/tripwires.py",
        "src/evallab/hackbook/prefix_checks.py",
        "src/evallab/trace_anatomy.py",
        "src/evallab/task_status.py",
        "src/evallab/line/replay_sets.json",
        "research/experiments/mimo-defect-map-v1/inventory.json",
        "scripts/line-gate.py",
        "agents/ship-gates.toml",
        "Makefile",
        "pyproject.toml",
        "uv.lock",
        "policy/standing-approvals.yaml",
    ],
)
def test_decision_dependencies_and_gate_controls_trigger(repository, path):
    root, _ = repository
    assert path in gate.line_paths(root, {path})


@pytest.mark.parametrize(
    "path",
    [
        "docs/line.md",
        "docs/hack-drop.md",
        "research/experiments/unrelated/analysis.py",
        "research/experiments/ov1106-outcome/results/line-replay.json",
        "src/evallab/cli.py",
        "src/evallab/cohort.py",
    ],
)
def test_docs_and_unrelated_analysis_do_not_trigger(repository, path):
    root, _ = repository
    assert path not in gate.line_paths(root, {path})


def test_paths_receipt_covers_new_deleted_and_renamed_revision_paths(
    tmp_path, repository
):
    import json

    root, run = repository
    before = {
        "src/evallab/line/deleted.py",
        "src/evallab/line/moved-out.py",
        "research/experiments/unrelated/moved-in.py",
    }
    for name in before:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not Python; path matching does not parse code\n")
    run("add", *sorted(before))
    run("commit", "--quiet", "-m", "base decision paths")
    run("update-ref", "refs/remotes/origin/main", "HEAD")
    (root / "src/evallab/line/deleted.py").unlink()
    docs = root / "docs"
    docs.mkdir()
    (root / "src/evallab/line/moved-out.py").rename(docs / "moved-out.md")
    (root / "research/experiments/unrelated/moved-in.py").rename(
        root / "src/evallab/line/moved-in.py"
    )
    (root / "src/evallab/line/added.py").write_text("new decision implementation\n")
    after = {
        "docs/moved-out.md",
        "src/evallab/line/moved-in.py",
        "src/evallab/line/added.py",
    }
    run("add", "--update")
    run("add", *sorted(after))
    run("commit", "--quiet", "-m", "change decision paths")
    # A dirty source and an untracked decision file must not require parsing.
    (root / "scripts/line-replay.py").write_text("invalid Python !!!\n")
    (root / "src/evallab/line/untracked.py").write_text("")
    output = tmp_path / "revision-paths"
    assert gate.main(["--paths", "--output", str(output)]) == 0
    report = json.loads((output / "gate.json").read_text())
    assert report["verdict"] == "PATHS_ONLY"
    assert report["passed"] is False
    assert set(report["changed_line_paths"]) == {
        "src/evallab/line/deleted.py",
        "src/evallab/line/moved-out.py",
        "src/evallab/line/moved-in.py",
        "src/evallab/line/added.py",
    }
    assert "src/evallab/line/untracked.py" not in report["line_paths"]
