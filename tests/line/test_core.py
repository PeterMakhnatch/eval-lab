"""Synthetic batch contracts: no third-party rows or external runtime state."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest

from evallab import hack_drop as hd
from evallab.line import core
from evallab.line.io import digest, read_json, read_rows, write_json, write_rows
from evallab.line.spec import load_spec


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


def test_cached_stage_verdicts_and_queue_idempotence(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
                        "details": {
                            "suspicion": 80,
                            "verdict": "suspicious",
                            "model": "synthetic/model",
                            "attested": True,
                            "cost_usd": 0.0,
                        },
                    }
                },
            }
            for run_id in ("clean-run", "weak-run")
        ],
    )
    soft = hd.StageVerdict("l0", "review", ["L0_SOFT_TARGET_SURFACE_WRITE"], [], {})
    monkeypatch.setattr(hd, "evaluate_l0_stage", lambda _item: soft)  # eligible for a reading
    spec["autoreview"] = {"mode": "cached", "cache": str(cache), "max_usd": 0}
    spec["models"]["allowed"] = ["synthetic/model"]
    write_json(fixture_spec, spec)
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert core.check_batch(batch, fixture_spec.parent)["passed"]
    assert {r["decision"] for r in read_rows(batch / "decisions.jsonl")} == {"review"}
    spec["batch_id"] = "second"
    write_json(fixture_spec, spec)
    core.run_line(fixture_spec, fixture_spec.parent)
    assert len(read_rows(Path(spec["judge"]["queue"]))) == 2


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


def _count_evaluations(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    real = core._evaluate

    def counting(*args: object, **kwargs: object) -> object:
        calls.append(1)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(core, "_evaluate", counting)
    return calls


def test_run_computes_twice_and_standalone_check_once(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _count_evaluations(monkeypatch)
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert len(calls) == 2  # the two in-process passes; the trailing check reuses both
    assert read_json(batch / "check.json")["passed"]
    calls.clear()
    assert core.check_batch(batch, fixture_spec.parent)["passed"]
    assert len(calls) == 1  # one fresh recompute against the stored decisions


def test_standalone_check_catches_a_stage_that_changed_since_the_run(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    monkeypatch.setattr(hd, "evaluate_l0_stage", lambda _: hd.StageVerdict("l0", "drop"))
    result = core.check_batch(batch, fixture_spec.parent)
    assert not result["passed"]
    assert next(c for c in result["checks"] if c["name"] == "determinism")["status"] == "FAIL"


def test_fast_check_never_claims_determinism(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    calls = _count_evaluations(monkeypatch)
    result = core.check_batch(batch, fixture_spec.parent, fast=True)
    assert calls == []
    assert result["determinism_verified"] is False
    assert not result["passed"]
    determinism = next(c for c in result["checks"] if c["name"] == "determinism")
    assert determinism["status"] == "SKIPPED"
    assert "determinism not verified" in determinism["details"]
    assert {c["status"] for c in result["checks"] if c["name"] != "determinism"} == {"PASS"}


def test_fast_check_exits_three_and_leaves_the_verified_artifacts(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from evallab import cli

    monkeypatch.setattr(cli, "instrument_openinference", lambda: None)
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    before = {name: digest(batch / name) for name in ("check.json", "scoreboard.json")}
    code = cli.run_cli(
        ["line", "check", str(batch), "--fast", "--json"], workspace=fixture_spec.parent
    )
    assert code == 3
    assert "determinism not verified" in capsys.readouterr().out
    assert before == {name: digest(batch / name) for name in before}
    write_rows(batch / "judge-queue.jsonl", [])
    assert cli.run_cli(["line", "check", str(batch), "--fast"], workspace=fixture_spec.parent) == 2


@pytest.mark.parametrize("reward,expected", [(1.0, 1.0), (0.0, 0.0), (None, None)])
def test_native_harbor_reward_and_unknown(
    fixture_spec: Path, reward: float | None, expected: float | None
) -> None:
    spec = read_json(fixture_spec)
    manifest = Path(spec["sources"][0]["rollouts"])
    rows = read_rows(manifest)
    native = {"split": "dev", "verifier_result": {"rewards": {}}}
    if reward is not None:
        native["verifier_result"]["rewards"]["reward"] = reward
    bundle = fixture_spec.parent / "native"
    write_json(bundle / "result.json", native)
    (bundle / "solution.py").write_text("def f(): return 1\n")
    rows[0]["path"] = str(bundle)
    rows[0].pop("reward")
    write_rows(manifest, rows)
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert read_rows(batch / "runs.jsonl")[0]["reward"] == expected
    assert core.check_batch(batch, fixture_spec.parent)["passed"]
    assert read_json(batch / "scoreboard.json")["task_side"]["reward1_uncertified"] == 0
    rows[0]["task_id"] = "unknown-uncertified-task"
    spec["batch_id"] = f"native-{str(reward).lower()}"
    write_json(fixture_spec, spec)
    write_rows(manifest, rows)
    uncertified = core.run_line(fixture_spec, fixture_spec.parent)
    assert read_json(uncertified / "scoreboard.json")["task_side"]["reward1_uncertified"] == int(
        expected == 1.0
    )


def test_interrupted_check_never_publishes_pass(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("interrupted before gates completed")

    monkeypatch.setattr(core, "check_batch", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        core.run_line(fixture_spec, fixture_spec.parent)
    spec = read_json(fixture_spec)
    result = read_json(Path(spec["out_root"]) / spec["batch_id"] / "check.json")
    assert not result["passed"]
    assert {check["status"] for check in result["checks"]} == {"PENDING"}


def test_scoreboard_receipt_times_the_scoreboard_build(
    fixture_spec: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = hd.load_labels

    def slow(*args: object, **kwargs: object) -> object:
        time.sleep(0.05)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(hd, "load_labels", slow)
    batch = core.run_line(fixture_spec, fixture_spec.parent)
    assert read_json(batch / "receipts" / "scoreboard.json")["seconds"] >= 0.05
    assert core.check_batch(batch, fixture_spec.parent)["passed"]
