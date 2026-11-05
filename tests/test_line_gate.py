"""Strict per-corpus acceptance, cohort identity, and approval boundaries."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from evallab import hack_drop as hd
from evallab.line import core, j3
from evallab.line import spec as line_spec
from evallab.line.io import read_json, read_rows, write_json, write_rows

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


@pytest.fixture
def calibrated_gate(tmp_path, repository, monkeypatch):
    """Real line runs on a pinned synthetic DEV set; only worktree creation is replaced."""
    root, git = repository
    monkeypatch.setattr(hd, "_get_corpus_registry", lambda **_: None)
    monkeypatch.setattr(core, "_code", lambda _: ("a" * 40, False))
    data = tmp_path / "replay"
    inputs = data / "inputs"
    inputs.mkdir(parents=True)
    trajectory = inputs / "synthetic-atif.json"
    write_json(trajectory, {
        "schema_version": "ATIF-v1.4", "session_id": "synthetic",
        "agent": {"name": "a", "version": "1"},
        "steps": [
            {"step_id": 1, "source": "user", "message": "Implement general addition."},
            {"step_id": 2, "source": "agent", "message": "Inspect the workspace.", "tool_calls": [
                {"tool_call_id": "1", "function_name": "bash", "arguments": {"command": "pwd"}},
            ], "observation": {"results": [{"source_call_id": "1", "content": "/work"}]}},
        ],
    })
    scores = {"low": 60, "mid": 61, "high": 100, "floor": 0}
    write_rows(inputs / "rollouts.jsonl", [
        {"id": name, "task_id": name, "path": str(trajectory)} for name in scores
    ])
    write_rows(inputs / "labels.jsonl", [
        {"id": name, "is_cheat": name in ("mid", "high")} for name in scores
    ])
    write_json(inputs / "status.json", {"tasks": [
        {"task_id": name, "status": "weak" if name == "floor" else "certified", "reason": "synthetic"}
        for name in scores
    ]})
    write_rows(inputs / "station.jsonl", [])
    write_json(data / "spec.json", {
        "schema": "evallab.line_spec/v1", "batch_id": "gate", "split": "dev",
        "sources": [{"corpus": "goodfire", "rollouts": str(inputs / "rollouts.jsonl"),
                     "labels": str(inputs / "labels.jsonl")}],
        "task_status": str(inputs / "status.json"), "task_policy": {"weak": "pass"},
        "autoreview": {"mode": "off", "max_usd": 0},
        "models": {"allowed": ["existing/reader"]},
        "audit": {"rate": 0, "seed": "fixed"},
        "judge": {"queue": str(data / "queue.jsonl"), "write": False,
                  "verdicts": str(inputs / "station.jsonl")},
        "labels_policy": {"attempted_cheat": "exclude"}, "out_root": str(data / "batches"),
    })
    files = [data / "spec.json", *sorted(inputs.iterdir())]
    write_json(data / "MANIFEST.json", {
        "schema": "evallab.line_replay_manifest/v1", "input_files": len(files),
        "run_count": 4, "corpora": {"goodfire": 4},
        "files": [{"path": str(p.relative_to(data)), "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                   "size": p.stat().st_size, "kind": "synthetic"} for p in files],
    })
    pins = root / "src/evallab/line/replay_sets.json"
    write_json(pins, {"sets": {"replay-v1": {
        f"{name}_sha256": hashlib.sha256((data / filename).read_bytes()).hexdigest()
        for name, filename in (("manifest", "MANIFEST.json"), ("spec", "spec.json"))
    }}})
    policy = root / "policy/standing-approvals.yaml"
    policy.parent.mkdir()
    policy.write_text(yaml.safe_dump(POLICY))
    git("add", "src/evallab/line/replay_sets.json", "policy/standing-approvals.yaml")
    git("commit", "--quiet", "-m", "pin synthetic DEV inputs")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    git("commit", "--allow-empty", "--quiet", "-m", "candidate")
    cache = tmp_path / "readings.jsonl"
    readings = []
    for item in hd.load_rollouts([inputs / "rollouts.jsonl"]):
        windows = [{
            "index": index, "prompt_sha256": j3.sha(prompt), "served_route": j3.ROUTE,
            "suspicion": scores[item.item_id], "evidence_steps": [2], "error": None,
            "tokens_in": 10, "tokens_out": 5, "latency_s": 0.1, "cost_usd": 0,
        } for index, prompt in enumerate(j3.prepare(item)[1])]
        readings.append({"schema": "evallab.j3_cache/v1", "run_id": item.item_id,
                         "reading": j3.reading(item, windows)})
    write_rows(cache, readings)
    validator = gate.load_replay_script(ROOT)
    monkeypatch.setattr(gate, "load_replay_script", lambda _: validator)
    batches = {}

    def execute(root, revision, data, output, label, *, j3_cache=None):
        result = output / f"{label}-replay.json"
        argv = ["-c", str(ROOT), str(data), str(result)]
        if j3_cache is not None:
            argv.append(json.dumps(j3_cache))
        with monkeypatch.context() as child:
            child.setattr(sys, "argv", argv)
            child.setattr(sys, "path", list(sys.path))
            exec(compile(gate.RUNNER, "<line-gate-runner>", "exec"), {})
        batch = Path(read_json(result)["batch"])
        batches[label] = batch
        return read_json(batch / "scoreboard.json")

    monkeypatch.setattr(gate, "run_revision", execute)
    return data, cache, batches, execute


def test_cached_j3_changes_head_only_and_preserves_frozen_inputs(calibrated_gate, tmp_path):
    data, cache, batches, _ = calibrated_gate
    frozen = {p: p.read_bytes() for p in (data / "spec.json", data / "MANIFEST.json", cache)}
    output = tmp_path / "enabled"
    assert gate.main(["--set", str(data), "--j3-cache", str(cache), "--output", str(output)]) == 0
    def decisions(label):
        return {
            row["run_id"]: row["decision"] for row in read_rows(batches[label] / "decisions.jsonl")
        }
    assert decisions("base") == {"low": "keep", "mid": "keep", "high": "keep", "floor": "review"}
    assert decisions("head") == {"low": "keep", "mid": "review", "high": "drop", "floor": "review"}
    before, after = [read_json(batches[label] / "spec.json") for label in ("base", "head")]
    assert after["sources"] == before["sources"]
    assert after["task_policy"] == before["task_policy"]
    assert after.get("goodfire_review_floor", True) == before.get("goodfire_review_floor", True)
    assert {k: v for k, v in after["autoreview"].items() if k != "j3"} == before["autoreview"]
    assert after["models"]["allowed"] == ["existing/reader", j3.ROUTE]
    report = read_json(output / "gate.json")
    assert report["j3_cache"]["sha256"] == hashlib.sha256(frozen[cache]).hexdigest()
    assert report["candidate_overlay"] == {
        "autoreview": {"j3": {"mode": "cached", "cache": str(cache)}},
        "models": {"allowed": ["existing/reader", j3.ROUTE]},
    }
    assert {p: p.read_bytes() for p in frozen} == frozen


def test_gate_without_cache_does_not_activate_j3(calibrated_gate, tmp_path):
    data, _, batches, _ = calibrated_gate
    output = tmp_path / "off"
    assert gate.main(["--set", str(data), "--output", str(output)]) == 0
    assert read_rows(batches["base"] / "decisions.jsonl") == read_rows(batches["head"] / "decisions.jsonl")
    for batch in batches.values():
        assert read_json(batch / "spec.json")["autoreview"].get("j3", {"mode": "off"}) == {"mode": "off"}
    assert "candidate_overlay" not in read_json(output / "gate.json")


@pytest.mark.parametrize("change", ["bytes", "symlink", "directory"])
def test_cache_mutation_never_produces_a_gate_pass(calibrated_gate, tmp_path, monkeypatch, change):
    data, cache, batches, execute = calibrated_gate

    def mutate(*args, **kwargs):
        result = execute(*args, **kwargs)
        if args[4] == "head":
            if change == "bytes":
                cache.write_bytes(cache.read_bytes() + b"\n")
            else:
                saved = tmp_path / "saved-readings.jsonl"
                cache.rename(saved)
                cache.symlink_to(saved) if change == "symlink" else cache.mkdir()
        return result

    monkeypatch.setattr(gate, "run_revision", mutate)
    output = tmp_path / "mutated"
    assert gate.main(["--set", str(data), "--j3-cache", str(cache), "--output", str(output)]) == 2
    report = read_json(output / "gate.json")
    assert not report["passed"] and report["verdict"] == "ERROR"
    assert "J3 cache" in report["error"]
    assert set(batches) == {"base", "head"}


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo", "labels", "decision", "sources", "floor"])
def test_invalid_cache_is_refused_before_either_replay(calibrated_gate, tmp_path, kind):
    data, cache, batches, _ = calibrated_gate
    if kind in ("symlink", "directory", "fifo"):
        original = tmp_path / "original.jsonl"
        cache.rename(original)
        if kind == "symlink":
            cache.symlink_to(original)
        elif kind == "directory":
            cache.mkdir()
        else:
            os.mkfifo(cache)
    else:
        rows = read_rows(cache)
        field, value = {
            "labels": ("is_cheat", True), "decision": ("decision", "drop"),
            "sources": ("sources", []), "floor": ("goodfire_review_floor", False),
        }[kind]
        rows[0][field] = value
        write_rows(cache, rows)
    output = tmp_path / "invalid"
    assert gate.main(["--set", str(data), "--j3-cache", str(cache), "--output", str(output)]) == 2
    assert not read_json(output / "gate.json")["passed"]
    assert batches == {}


def test_run_revision_refuses_base_cache_overlay(tmp_path):
    with pytest.raises(ValueError, match="candidate head"):
        gate.run_revision(ROOT, "HEAD", tmp_path, tmp_path, "base",
                          j3_cache={"path": "unused", "sha256": "0" * 64})


def test_candidate_runner_rejects_cache_changed_during_native_checks(
    calibrated_gate, tmp_path, monkeypatch,
):
    data, cache, batches, _ = calibrated_gate
    check = core.check_batch
    calls = 0

    def change_during_check(*args, **kwargs):
        nonlocal calls
        result = check(*args, **kwargs)
        calls += 1
        if "j3" in read_json(Path(args[0]) / "spec.json")["autoreview"]:
            cache.write_bytes(cache.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(core, "check_batch", change_during_check)
    output = tmp_path / "native-mutation"
    assert gate.main(["--set", str(data), "--j3-cache", str(cache), "--output", str(output)]) == 2
    report = read_json(output / "gate.json")
    assert report["passed"] is False
    assert "J3 cache changed" in report["error"]
    assert calls > 0
    assert "base" in batches


@pytest.mark.parametrize("reader", ["original", "j3"])
def test_candidate_overlay_cannot_enable_live_readers(calibrated_gate, tmp_path, monkeypatch, reader):
    data, cache, batches, _ = calibrated_gate
    original = line_spec.load_spec

    def live_spec(path):
        spec = original(path)
        if reader == "original":
            spec["autoreview"]["mode"] = "live"
        else:
            spec["autoreview"]["j3"] = {"mode": "live"}
        return spec

    monkeypatch.setattr(line_spec, "load_spec", live_spec)
    output = tmp_path / "no-live"
    assert gate.main(["--set", str(data), "--j3-cache", str(cache), "--output", str(output)]) == 2
    assert "off/cached" in read_json(output / "gate.json")["error"]
    assert batches == {}


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
