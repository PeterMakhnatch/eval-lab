"""Synthetic batch contracts: no third-party rows or external runtime state."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from evallab import hack_drop as hd
from evallab.line import core
from evallab.line.io import digest, read_json, read_rows, write_json, write_rows
from evallab.line.spec import load_spec


@pytest.fixture
def fixture_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(hd, "_get_corpus_registry", lambda **_: None)
    monkeypatch.setattr(core, "_code", lambda _: ("a" * 40, False))
    clean = tmp_path / "clean.py"
    clean.write_text("def add(a, b):\n    return a + b\n")
    manifest = tmp_path / "rollouts.jsonl"
    write_rows(
        manifest,
        [
            {"id": "clean-run", "task_id": "plain-task", "path": str(clean), "reward": 1},
            {"id": "weak-run", "task_id": "weak-task", "path": str(clean)},
        ],
    )
    labels = tmp_path / "labels.jsonl"
    write_rows(
        labels, [{"id": "clean-run", "is_cheat": False}, {"id": "weak-run", "is_cheat": True}]
    )
    task_status = tmp_path / "status.json"
    write_json(
        task_status,
        {
            "tasks": [
                {"task_id": "plain-task", "status": "certified", "reason": "ALL_GATES_PASS"},
                {"task_id": "weak-task", "status": "weak", "reason": "ATTACK_ACCEPTED"},
            ]
        },
    )
    path = tmp_path / "spec.json"
    write_json(
        path,
        {
            "schema": "evallab.line_spec/v1",
            "batch_id": "synthetic",
            "split": "dev",
            "sources": [{"corpus": "synthetic", "rollouts": str(manifest), "labels": str(labels)}],
            "task_status": str(task_status),
            "task_action": "review",
            "autoreview": {"mode": "off", "max_usd": 0},
            "models": {"allowed": []},
            "audit": {"rate": 1, "seed": "fixed"},
            "judge": {
                "queue": str(tmp_path / "queue.jsonl"),
                "write": True,
                "verdicts": str(tmp_path / "no-verdicts.jsonl"),
            },
            "labels_policy": {"attempted_cheat": "exclude"},
            "out_root": str(tmp_path / "batches"),
        },
    )
    return path


def test_full_line_routes_reviews_and_audits(fixture_spec: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    check = core.check_batch(batch, fixture_spec.parent)
    assert check["passed"], check
    assert len(check["checks"]) == 8
    decisions = read_rows(batch / "decisions.jsonl")
    assert [(r["run_id"], r["decision"], r["audit"]) for r in decisions] == [
        ("clean-run", "keep", True),
        ("weak-run", "review", False),
    ]
    assert "AUDIT_SAMPLE" in decisions[0]["reasons"]
    assert read_rows(batch / "verdicts.jsonl") == []
    assert len(read_rows(batch / "judge-queue.jsonl")) == 2
    assert read_json(batch / "scoreboard.json")["overall"]["counts"] == {
        "keep": 1,
        "drop": 0,
        "review": 1,
    }
    assert len(read_rows(batch / "export" / "kept.jsonl")) == 1


def test_audit_exact_hash_boundary() -> None:
    probability = int(hashlib.sha256(b"fixed:sample").hexdigest()[:8], 16) / 2**32
    assert not core.audit_sample("fixed", "sample", probability)
    assert core.audit_sample("fixed", "sample", probability + 1 / 2**32)
    assert not core.audit_sample("fixed", "sample", 0)
    assert core.audit_sample("fixed", "sample", 1)


@pytest.mark.parametrize(
    "artifact,mutate,failed_gate",
    [
        ("decisions.jsonl", lambda rows: rows + rows[:1], "conservation"),
        ("decisions.jsonl", lambda rows: [{**r, "label": True} for r in rows], "label_isolation"),
        ("judge-queue.jsonl", lambda rows: rows[:1], "judge_routing"),
        ("runs.jsonl", lambda rows: [{**r, "sha256": "changed"} for r in rows], "dev_only"),
    ],
)
def test_checks_fail_closed_on_corruption(
    fixture_spec: Path, artifact: str, mutate, failed_gate: str
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    write_rows(batch / artifact, mutate(read_rows(batch / artifact)))
    result = core.check_batch(batch, fixture_spec.parent)
    assert not result["passed"]
    assert next(c for c in result["checks"] if c["name"] == failed_gate)["status"] == "FAIL"
    assert next(c for c in result["checks"] if c["name"] == "provenance")["status"] == "FAIL"


def test_scoreboard_tampering_is_detected(fixture_spec: Path) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    score = read_json(batch / "scoreboard.json")
    score["overall"]["runs"] += 1
    write_json(batch / "scoreboard.json", score)
    checks = core.check_batch(batch, fixture_spec.parent)["checks"]
    assert next(c for c in checks if c["name"] == "scoreboard_recompute")["status"] == "FAIL"


def test_cached_stage_verdicts_and_queue_idempotence(fixture_spec: Path) -> None:
    spec = read_json(fixture_spec)
    cache = fixture_spec.parent / "cache"
    write_rows(
        cache / "review.jsonl",
        [
            {
                "id": run_id,
                "stage_decisions": {
                    "autoreview": {
                        "decision": "review",
                        "reasons": ["AUTOREVIEW_SUSPICION_80"],
                        "details": {"suspicion": 80},
                    }
                },
            }
            for run_id in ("clean-run", "weak-run")
        ],
    )
    spec["autoreview"] = {"mode": "cached", "cache": str(cache), "max_usd": 0}
    write_json(fixture_spec, spec)
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert core.check_batch(batch, fixture_spec.parent)["passed"]
    assert {r["decision"] for r in read_rows(batch / "decisions.jsonl")} == {"review"}
    spec["batch_id"] = "second"
    write_json(fixture_spec, spec)
    core.run_line(fixture_spec, fixture_spec.parent)
    assert len(read_rows(Path(spec["judge"]["queue"]))) == 2


def test_missing_cached_verdict_refuses(fixture_spec: Path) -> None:
    spec = read_json(fixture_spec)
    cache = fixture_spec.parent / "cache"
    write_rows(
        cache / "review.jsonl",
        [{"id": "clean-run", "stage_decisions": {"autoreview": {"decision": "pass"}}}],
    )
    spec["autoreview"] = {"mode": "cached", "cache": str(cache), "max_usd": 0}
    write_json(fixture_spec, spec)
    with pytest.raises(ValueError, match="missing cached"):
        core.run_line(fixture_spec, fixture_spec.parent)


@pytest.mark.parametrize(
    "mutation",
    [
        {"split": "test"},
        {"autoreview": {"mode": "live", "max_usd": 1, "route": "not-allowed", "approval": "yes"}},
        {"autoreview": {"mode": "live", "max_usd": 0, "route": "ok", "approval": "yes"}},
        {"autoreview": {"mode": "live", "max_usd": 1, "route": "ok"}},
        {"audit": {"rate": -1, "seed": "x"}},
        {"batch_id": "../escape"},
    ],
)
def test_spec_rejects_unsafe_inputs(fixture_spec: Path, mutation: dict) -> None:
    spec = read_json(fixture_spec)
    spec["models"]["allowed"] = ["ok"]
    spec.update(mutation)
    write_json(fixture_spec, spec)
    with pytest.raises(ValueError):
        load_spec(fixture_spec)


def test_non_dev_manifest_aborts_before_queue(fixture_spec: Path) -> None:
    spec = read_json(fixture_spec)
    manifest = Path(spec["sources"][0]["rollouts"])
    rows = read_rows(manifest)
    rows[1]["split"] = "test"
    write_rows(manifest, rows)
    with pytest.raises(Exception, match="non-DEV"):
        core.run_line(fixture_spec, fixture_spec.parent)
    assert not Path(spec["judge"]["queue"]).exists()
    assert not (Path(spec["out_root"]) / spec["batch_id"]).exists()


def test_decisions_remain_label_blind(fixture_spec: Path) -> None:
    spec = read_json(fixture_spec)
    first = core.run_line(fixture_spec, fixture_spec.parent)
    labels = Path(spec["sources"][0]["labels"])
    write_rows(
        labels, [{"id": "clean-run", "is_cheat": True}, {"id": "weak-run", "is_cheat": False}]
    )
    spec["batch_id"] = "flipped-labels"
    write_json(fixture_spec, spec)
    second = core.run_line(fixture_spec, fixture_spec.parent)
    assert digest(first / "decisions.jsonl") == digest(second / "decisions.jsonl")
    assert (
        read_json(first / "scoreboard.json")["overall"]
        != read_json(second / "scoreboard.json")["overall"]
    )


def test_station_undo_records_are_not_imported(fixture_spec: Path) -> None:
    spec = read_json(fixture_spec)
    verdicts = Path(spec["judge"]["verdicts"])
    write_rows(
        verdicts,
        [
            {
                "schema": "judge-verdict/v1",
                "id": "one",
                "run_id": "clean-run",
                "judge": "synthetic",
                "verdict": "cheat",
            },
            {"schema": "judge-undo/v1", "undoes": "one"},
            {
                "schema": "judge-verdict/v1",
                "id": "two",
                "run_id": "weak-run",
                "judge": "synthetic",
                "verdict": "legitimate",
            },
        ],
    )
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert [r["id"] for r in read_rows(batch / "verdicts.jsonl")] == ["two"]


def test_cli_run_check_scoreboard_and_failure_exit(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from evallab import cli

    monkeypatch.setattr(cli, "instrument_openinference", lambda: None)
    assert (
        cli.run_cli(["line", "run", str(fixture_spec), "--json"], workspace=fixture_spec.parent)
        == 0
    )
    spec = read_json(fixture_spec)
    batch = Path(spec["out_root"]) / spec["batch_id"]
    assert cli.run_cli(["line", "check", str(batch), "--json"], workspace=fixture_spec.parent) == 0
    assert (
        cli.run_cli(["line", "scoreboard", str(batch), "--json"], workspace=fixture_spec.parent)
        == 0
    )
    assert '"overall"' in capsys.readouterr().out
    write_rows(batch / "judge-queue.jsonl", [])
    assert cli.run_cli(["line", "check", str(batch), "--json"], workspace=fixture_spec.parent) == 2


def test_bundle_metadata_records_known_model_and_raw_reward(fixture_spec: Path) -> None:
    spec = read_json(fixture_spec)
    manifest = Path(spec["sources"][0]["rollouts"])
    rows = read_rows(manifest)
    bundle = fixture_spec.parent / "bundle"
    write_json(bundle / "run.json", {"split": "dev", "model": "synthetic/model", "reward": 1})
    (bundle / "solution.py").write_text("def f(): return 1\n")
    rows[0]["path"] = str(bundle)
    rows[0].pop("reward")
    write_rows(manifest, rows)
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    run = read_rows(batch / "runs.jsonl")[0]
    assert run["model"] == "synthetic/model"
    assert run["reward"] == 1
    assert core.check_batch(batch, fixture_spec.parent)["passed"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("spend_usd", 0.5),
        ("models", [{"role": "autoreview", "route": "not-allowed"}]),
    ],
)
def test_spend_and_models_fail_closed(fixture_spec: Path, field: str, value) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    receipt_path = batch / "receipts" / "trace.json"
    receipt = read_json(receipt_path)
    receipt[field] = value
    write_json(receipt_path, receipt)
    checks = core.check_batch(batch, fixture_spec.parent)["checks"]
    assert next(c for c in checks if c["name"] == "spend_and_models")["status"] == "FAIL"


def test_decision_recomputation_detects_nondeterministic_stage(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def changing_stage(_):
        nonlocal calls
        calls += 1
        return hd.StageVerdict("l0", "pass" if (calls - 1) // 2 % 2 == 0 else "drop")

    monkeypatch.setattr(hd, "evaluate_l0_stage", changing_stage)
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    result = read_json(batch / "check.json")
    assert not result["passed"]
    assert next(c for c in result["checks"] if c["name"] == "determinism")["status"] == "FAIL"
