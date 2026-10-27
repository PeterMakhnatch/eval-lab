"""Behavioral tests for the TW-P1 gate measurement runner (synthetic data only)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
RUN = REPO / "research/experiments/tripwires-gate-production-20261024/run.py"
_spec = importlib.util.spec_from_file_location("tw_p1_measure_run", RUN)
assert _spec and _spec.loader
run = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = run
_spec.loader.exec_module(run)
v3 = run.v3


# --------------------------------------------------------------------------- fixtures


def _assignments(tmp_path: Path, rows: list[tuple[str, str]]) -> Path:
    path = tmp_path / "assignments.csv"
    lines = ["key_id,item_id,task,unit,split,expected_verdict,exposures"]
    for task, split in rows:
        lines.append(f"tb4-audit-v1,item:{task},{task},terminal-bench/{task},{split},fail,")
    lines.append("other-key,item:z,z,z-unit,dev,fail,")
    path.write_text("\n".join(lines) + "\n")
    return path


def _v4(tmp_path: Path, tasks: list[str], flip: str | None = None) -> Path:
    assignment = run.split_tasks(tasks, run.V4_SEED)
    if flip:
        assignment[flip] = "holdout" if assignment[flip] == "tune" else "tune"
    path = tmp_path / "v4-results.json"
    path.write_text(json.dumps({"task_assignment": assignment}))
    return path


DEV = ["alpha", "bravo", "charlie", "delta"]
ROWS = [(t, "dev") for t in DEV] + [("echo", "test"), ("foxtrot", "dev"), ("foxtrot", "test")]


_LEGACY = run.split_tasks(DEV, run.V4_SEED)
SEALED = next(
    t for t in sorted(DEV) if _LEGACY[t] == "holdout"
)  # census task sealed by the registry
EFFECTIVE = [t for t in DEV if t != SEALED] + ["newdev"]  # newdev: effective DEV, never captured


def _manifest(tmp_path: Path) -> tuple[dict[str, Any], Path, Path]:
    assignments = _assignments(tmp_path, ROWS)
    v4 = _v4(tmp_path, DEV)
    return run.build_manifest(assignments, v4, EFFECTIVE), assignments, v4


def _write_atif(path: Path, steps: list[dict[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps({"steps": steps}).encode()
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _call(step_id: int, command: str = "ls", observation: str | None = "out") -> dict[str, Any]:
    step: dict[str, Any] = {
        "step_id": step_id,
        "source": "agent",
        "message": command,
        "tool_calls": [
            {
                "tool_call_id": f"c{step_id}",
                "function_name": "bash_command",
                "arguments": {"command": command},
            }
        ],
    }
    if observation is not None:
        step["observation"] = {"results": [{"content": observation}]}
    return step


def _report(**rules: list[dict[str, Any]]) -> dict[str, Any]:
    report: dict[str, Any] = {name: [] for name in v3.FLAG_RULES}
    report.update(rules)
    report["hackbook"] = []
    report["structural_checks"] = []
    return report


# ------------------------------------------------------------------------------ split


def test_manifest_keeps_v4_tune_and_puts_legacy_holdout_and_uncaptured_in_holdout(
    tmp_path: Path,
) -> None:
    manifest, assignments, v4 = _manifest(tmp_path)
    tune, holdout = set(manifest["tune_tasks"]), set(manifest["holdout_tasks"])
    legacy = run.v4_assignment(v4)
    assert tune | holdout == set(EFFECTIVE)
    assert not tune & holdout
    assert tune == {
        t for t in EFFECTIVE if legacy.get(t) == "tune"
    }  # no scored task moves into tune
    assert "newdev" in holdout and manifest["uncaptured_dev_tasks"] == ["newdev"]
    assert manifest["sealed_by_registry"] == [SEALED] and SEALED not in tune | holdout
    assert (
        manifest["n_census_tasks"],
        manifest["n_dev_tasks"],
        manifest["n_captured_effective"],
    ) == (4, 4, 3)
    # a TEST task and a task that also has a test row are never DEV
    assert "echo" not in tune | holdout and "foxtrot" not in tune | holdout
    assert run.verify_manifest(manifest, assignments, v4, EFFECTIVE) == manifest


def test_manifest_refuses_tampering_leaks_and_stale_sources(tmp_path: Path) -> None:
    manifest, assignments, v4 = _manifest(tmp_path)
    moved = dict(manifest, tune_tasks=manifest["tune_tasks"][:-1])
    with pytest.raises(ValueError, match="manifest_sha256"):
        run.verify_manifest(moved, assignments, v4, EFFECTIVE)

    leaked = dict(manifest, holdout_tasks=[*manifest["holdout_tasks"], manifest["tune_tasks"][0]])
    leaked["manifest_sha256"] = run.canonical_sha(
        {k: v for k, v in leaked.items() if k != "manifest_sha256"}
    )
    with pytest.raises(ValueError, match="split leak"):
        run.verify_manifest(leaked, assignments, v4, EFFECTIVE)

    unsealed = dict(manifest, holdout_tasks=[*manifest["holdout_tasks"], SEALED])
    unsealed["manifest_sha256"] = run.canonical_sha(
        {k: v for k, v in unsealed.items() if k != "manifest_sha256"}
    )
    with pytest.raises(ValueError, match="registry-sealed"):
        run.verify_manifest(unsealed, assignments, v4, EFFECTIVE)

    (tmp_path / "g").mkdir()
    changed = _assignments(tmp_path / "g", [*ROWS, ("golf", "dev")])
    with pytest.raises(ValueError, match="assignments changed|differ"):
        run.verify_manifest(manifest, changed, v4, EFFECTIVE)
    with pytest.raises(ValueError, match="effective DEV universe changed"):
        run.verify_manifest(manifest, assignments, v4, [*EFFECTIVE, "another"])


def test_manifest_refuses_a_v4_assignment_that_is_not_the_seeded_split(tmp_path: Path) -> None:
    assignments = _assignments(tmp_path, ROWS)
    with pytest.raises(ValueError, match="deterministic split"):
        run.build_manifest(assignments, _v4(tmp_path, DEV, flip="alpha"), EFFECTIVE)
    with pytest.raises(ValueError, match="differ"):
        run.build_manifest(assignments, _v4(tmp_path, [*DEV, "extra"]), EFFECTIVE)


def test_stage_guard_refuses_a_task_from_the_other_half(tmp_path: Path) -> None:
    manifest, _, _ = _manifest(tmp_path)
    tune = run.stage_tasks(manifest, "tune")
    holdout_task = manifest["holdout_tasks"][0]
    subject = v3.Subject(
        "m1", "dev", "positive", "pass:known_exploit", {}, source={"task": holdout_task}
    )
    with pytest.raises(ValueError, match="refuses non-tune"):
        run.assert_stage_subjects([subject], tune, "tune")
    sealed = v3.Subject("m2", "dev", "positive", "pass:known_exploit", {}, source={"task": SEALED})
    with pytest.raises(ValueError, match="refuses non-tune"):
        run.assert_stage_subjects([sealed], tune, "tune")
    with pytest.raises(ValueError, match="unknown TB4 stage"):
        run.stage_tasks(manifest, "test")


def _label_line(task: str, membership: str) -> str:
    return json.dumps(
        {"task": task, "split": "dev", "system": v3.TB4_SYSTEM, "membership_id": membership},
        separators=(",", ":"),
    )


def test_stage_labels_never_parse_lines_of_the_other_half(tmp_path: Path) -> None:
    path = tmp_path / "labels-all.jsonl"
    sealed = '{"task":"bravo", this line is not json'
    path.write_text(
        "\n".join([_label_line("alpha", "m1"), sealed, _label_line("alpha", "m2")]) + "\n"
    )
    rows, sha, counts = run.read_stage_labels(
        path, frozenset({"alpha"}), frozenset({"alpha", "bravo"})
    )
    assert [r["membership_id"] for r in rows] == ["m1", "m2"]
    assert counts == {"parsed_lines": 2, "sealed_lines_unparsed": 1}
    assert sha == hashlib.sha256(path.read_bytes()).hexdigest()

    path.write_text(_label_line("alpha", "m1") + "\n" + _label_line("zulu-test-task", "m9") + "\n")
    with pytest.raises(ValueError, match="non-DEV"):
        run.read_stage_labels(path, frozenset({"alpha"}), frozenset({"alpha", "bravo"}))


# ---------------------------------------------------------------------------- producer


def test_producer_requires_structural_checks_and_core_sections() -> None:
    report = _report()
    del report["structural_checks"]
    with pytest.raises(ValueError, match="structural_checks"):
        run.producer_record(report)
    report = _report()
    del report["grader_tamper"]
    with pytest.raises(ValueError, match="grader_tamper"):
        run.producer_record(report)


def test_structural_unavailable_is_not_clear_and_static_hits_need_no_trace_step() -> None:
    report = _report(grader_tamper=[{"step": 4}])
    report["structural_checks"] = [
        {
            "entry_id": "HB-TS05-001",
            "check": "c1",
            "status": "unavailable",
            "kind": "task_structure",
            "reason": "no grader",
        },
        {
            "entry_id": "HB-TS06-001",
            "check": "c2",
            "status": "hit",
            "kind": "task_structure",
            "evidence": [
                {"artifact_ref": "tests/test_x.py", "line": 3, "quote": "q", "sha256": "0" * 64}
            ],
        },
        {
            "entry_id": "HB-RS01-002",
            "check": "c3",
            "status": "hit",
            "kind": "agent_action",
            "evidence": [],
            "cited_steps": [7],
        },
    ]
    record = run.producer_record(report)
    by_check = {c["check"]: c for c in record["structural"]}
    assert by_check["c1"]["status"] == "unavailable" and by_check["c1"]["reason"] == "no grader"
    assert (
        by_check["c2"]["status"] == "hit"
        and by_check["c2"]["steps"] == []
        and by_check["c2"]["static_evidence"] == 1
    )
    assert by_check["c3"]["steps"] == [7]
    assert run.earliest_step(record) == 4

    report["structural_checks"][1]["evidence"] = []
    with pytest.raises(ValueError, match="cites no evidence"):
        run.producer_record(report)


def _record(producer: dict[str, Any], **extra: Any) -> dict[str, Any]:
    base = {
        "role": "positive",
        "source_label": "pass:known_exploit",
        "cluster": "t",
        "producer": producer,
        "policy": {"status": "measured", "hits": []},
        "consumer": {"status": "measured", "outcome": "allow", "decision": None},
        "timing": {"producer_first_fraction": 0.5, "gate_stop_fraction": 0.5},
    }
    return {**base, **extra}


def test_decisions_distinguish_unevaluated_from_clean() -> None:
    only_unavailable = {
        "rules": [{"rule": "history_browsing", "eligibility": "diagnostic", "gate": False}],
        "structural": [{"kind": "agent_action", "status": "unavailable", "gate": False}],
    }
    d = run.decisions_of(_record(only_unavailable))
    assert d["structural_agent_action"] is None  # unavailable is never clear
    assert d["core_verdict"] is False  # a diagnostic rule is not a verdict
    assert d["consumer_gate_deny"] is False

    hit = {"rules": [], "structural": [{"kind": "agent_action", "status": "hit", "gate": True}]}
    d = run.decisions_of(_record(hit))
    assert d["structural_agent_action"] is True and d["producer_cheating_any"] is True
    assert d["gate_flagged_producer"] is True and d["structural_task_structure"] is None

    fail_closed = _record(
        hit, consumer={"status": "measured", "outcome": "fail_closed", "decision": {"reason": "x"}}
    )
    assert run.decisions_of(fail_closed)["consumer_gate_deny"] is None
    errored = _record(hit, consumer={"status": "unavailable", "reason": "boom"})
    assert run.decisions_of(errored)["consumer_gate_deny"] is None
    deny = _record(hit, consumer={"status": "measured", "outcome": "deny", "decision": {"step": 3}})
    assert run.decisions_of(deny)["consumer_gate_deny"] is True


def test_decision_cell_counts_unknowns_outside_both_denominators() -> None:
    records = []
    values = [
        ("positive", True),
        ("positive", False),
        ("positive", None),
        ("negative", True),
        ("negative", False),
        ("other", True),
    ]
    for i, (role, _value) in enumerate(values):
        records.append(
            _record({"rules": [], "structural": []}, role=role, cluster=f"c{i}", source_label=role)
        )
    decisions = {id(r): {"core_verdict": v} for r, (_, v) in zip(records, values, strict=True)}
    cell = run.decision_cell(
        records, "core_verdict", decisions=decisions, label="t", bootstrap=False
    )
    assert (cell["true_positives"], cell["positives"]) == (1, 2)
    assert (cell["false_alarms"], cell["negatives"]) == (1, 2)
    assert (cell["other_fired"], cell["other_n"]) == (1, 1)
    assert cell["unknown"] == {"positive": 1}
    assert cell["precision"]["hits"] == 1 and cell["precision"]["n"] == 2


def test_cluster_bootstrap_is_deterministic_and_needs_two_clusters() -> None:
    clusters = {"a": [1, 0, 1, 2], "b": [0, 1, 1, 3], "c": [1, 1, 1, 1]}
    assert run.cluster_bootstrap(clusters, "x") == run.cluster_bootstrap(clusters, "x")
    assert run.cluster_bootstrap({"a": [1, 0, 1, 2]}, "x") is None


# ----------------------------------------------------------------------------- causal


def test_causal_recheck_catches_a_rule_that_needs_future_steps() -> None:
    steps = [_call(i, "bad" if i == 2 else "ok") for i in range(1, 5)]

    def causal(
        agent: list[dict[str, Any]], _steps: list[dict[str, Any]], _ctx: Any
    ) -> dict[str, Any]:
        return _report(
            grader_tamper=[{"step": s["step_id"]} for s in agent if s["message"] == "bad"]
        )

    def leaky(
        agent: list[dict[str, Any]], _steps: list[dict[str, Any]], _ctx: Any
    ) -> dict[str, Any]:
        # only fires at step 2 once step 4 exists: a future-dependent signal
        return _report(grader_tamper=[{"step": 2}] if len(agent) >= 4 else [])

    ok = run.causal_recheck(steps, run.producer_record(causal(steps, steps, None)), causal)
    assert ok["checked"] and ok["consistent"]
    bad = run.causal_recheck(steps, run.producer_record(leaky(steps, steps, None)), leaky)
    assert bad["checked"] and not bad["consistent"] and "missing_on_prefix" in bad["violations"][0]

    silent = run.causal_recheck(steps, run.producer_record(_report()), causal)
    assert silent == {
        "checked": False,
        "reason": "no_trace_cited_hits",
        "consistent": True,
        "violations": [],
    }


# --------------------------------------------------------------------------- consumer


class FakeWorker:
    def __init__(self, deny_at: int | None = None, reason: str = "tripwire_blocked") -> None:
        self.deny_at, self.reason = deny_at, reason
        self.gated: list[dict[str, Any]] = []
        self.ingested: list[int] = []

    def pre_action_gate(self, run_id: str, step: dict[str, Any]) -> dict[str, Any]:
        self.gated.append(step)
        allow = step["step_id"] != self.deny_at
        return {
            "allow": allow,
            "reason": "allow" if allow else self.reason,
            "triggered_rules": [] if allow else ["grader_tamper"],
            "run_state": "running",
            "gate_hits": []
            if allow
            else [
                {
                    "source": "policy",
                    "rule": "p",
                    "step": step["step_id"],
                    "entry_ids": ["HB-1"],
                    "kind": "agent_action",
                    "evidence": "SECRET",
                }
            ],
        }

    def ingest_event(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        self.ingested.append(event["payload"]["step"]["step_id"])
        return []

    def close(self) -> None:
        return None


def _steps() -> list[dict[str, Any]]:
    return [{"step_id": 1, "source": "user", "message": "task"}, _call(2), _call(3)]


def test_replay_stops_at_a_deny_and_never_ingests_the_denied_action() -> None:
    worker = FakeWorker(deny_at=3)
    result = run.replay_consumer(_steps(), "r", lambda events, ctx: worker)
    assert result["outcome"] == "deny"
    assert result["decision"]["step"] == 3 and result["decision"]["triggered_rules"] == [
        "grader_tamper"
    ]
    assert worker.ingested == [1, 2]
    assert [s["step_id"] for s in worker.gated] == [2, 3]
    assert all("observation" not in s for s in worker.gated)  # future observation withheld
    assert "evidence" not in result["decision"]["gate_hits"][0]  # no actor text retained


def test_replay_separates_fail_closed_from_deny_and_reports_errors_as_unavailable() -> None:
    closed = run.replay_consumer(
        _steps(),
        "r",
        lambda e, c: FakeWorker(deny_at=2, reason="deterministic_detector_unavailable"),
    )
    assert closed["outcome"] == "fail_closed"
    allowed = run.replay_consumer(_steps(), "r", lambda e, c: FakeWorker())
    assert (
        allowed["outcome"] == "allow"
        and allowed["n_gate_calls"] == 2
        and allowed["n_ingested"] == 3
    )

    class Boom(FakeWorker):
        def ingest_event(self, event: dict[str, Any]) -> list[dict[str, Any]]:
            raise RuntimeError("detector exploded")

    errored = run.replay_consumer(_steps(), "r", lambda e, c: Boom())
    assert errored["status"] == "unavailable" and "detector exploded" in errored["reason"]

    out_of_order = [{"step_id": 2, "source": "user"}, {"step_id": 1, "source": "user"}]
    assert (
        run.replay_consumer(out_of_order, "r", lambda e, c: FakeWorker())["status"] == "unavailable"
    )


def test_latency_summary_counts_calls_over_the_target() -> None:
    summary = run.latency_summary([10.0, 20.0, 2500.0])
    assert summary["n"] == 3 and summary["over_target"] == 1 and summary["max_ms"] == 2500.0
    assert run.latency_summary([]) == {"n": 0}


# --------------------------------------------------------------------------- measuring


def test_measure_subjects_keeps_unknowns_unknown_and_binds_hashes(tmp_path: Path) -> None:
    cohort = v3.cohort_by_id("cheatbench-actions-v1")
    good = tmp_path / "good/agent/trajectory.json"
    digest = _write_atif(good, _steps())
    bad = tmp_path / "bad/agent/trajectory.json"
    _write_atif(bad, _steps())
    unavailable = v3.ViewInput(None, None, "no_paired_actions_view")
    subjects = [
        v3.Subject(
            "ok",
            "test",
            "positive",
            "fail",
            {"full": v3.ViewInput(good, digest), "actions": unavailable},
        ),
        v3.Subject(
            "moved",
            "test",
            "negative",
            "pass",
            {"full": v3.ViewInput(bad, "0" * 64), "actions": unavailable},
        ),
        v3.Subject(
            "nolabel",
            "test",
            None,
            None,
            {"full": unavailable, "actions": unavailable},
            unknown_reason="label_unmapped",
        ),
    ]
    seen: list[int] = []

    def evaluator(steps: list[dict[str, Any]]) -> dict[str, Any]:
        seen.append(len(steps))
        return {"marker": True}

    records = run.measure_subjects(cohort, "test", subjects, evaluator=evaluator)
    by = {(r["item_id"], r["view"]): r for r in records}
    assert by[("ok", "full")]["status"] == "measured" and by[("ok", "full")]["marker"]
    assert by[("ok", "actions")]["unknown_reason"] == "no_paired_actions_view"
    assert by[("moved", "full")]["unknown_reason"] == "input_hash_mismatch"
    assert by[("nolabel", "full")]["unknown_reason"] == "label_unmapped"
    assert seen == [3]  # only the hash-verified trajectory reached the evaluator


# ------------------------------------------------------------------------ entry labels


def test_tb4_entry_labels_join_mechanism_to_entry_through_the_pm_finding(tmp_path: Path) -> None:
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            [
                {"task": "t", "finding": "f1", "id": "t/m1"},
                {"task": "t", "finding": "f2", "id": "t/m2"},
            ]
        )
    )
    entries = tmp_path / "entries.jsonl"
    entries.write_text(
        json.dumps(
            {
                "id": "HB-A",
                "examples": [
                    {"source": "preference_model_tb4", "item_or_task_id": "pm-tb4:t:f1"},
                    {"source": "elsewhere", "item_or_task_id": "pm-tb4:t:f2"},
                ],
            }
        )
        + "\n"
    )
    assert run.tb4_entry_labels(registry, entries) == {"t/m1": ["HB-A"]}


def test_entry_recall_needs_compatible_labels() -> None:
    producer = {
        "rules": [{"rule": "hb_x", "eligibility": "verdict", "gate": False}],
        "structural": [],
    }
    rec = _record(producer, source={"source_mechanism_labels": ["t/m1"]})
    rule_entries = {"hb_x": ["HB-A"]}
    mapped = run.entry_cells([rec], ["HB-A", "HB-B"], {}, {"t/m1": ["HB-A"]}, rule_entries)
    assert mapped["HB-A"]["recall_against_compatible_labels"]["status"] == "measured"
    assert mapped["HB-A"]["recall_against_compatible_labels"]["hits"] == 1
    assert mapped["HB-B"]["recall_against_compatible_labels"]["status"] == "no_compatible_labels"
    assert mapped["HB-B"]["accounted"] is False
    unmapped = run.entry_cells([rec], ["HB-A"], {}, None, rule_entries)
    assert (
        unmapped["HB-A"]["recall_against_compatible_labels"]["reason"]
        == "no entry label map supplied"
    )
    assert unmapped["HB-A"]["by_role"]["positive"]["action_hit"] == 1  # firing is still reported


# ---------------------------------------------------------------------- task packages


def test_package_reader_skips_cheat_and_marks_unreadable_roots_incomplete(tmp_path: Path) -> None:
    task = tmp_path / "t"
    (task / "tests").mkdir(parents=True)
    (task / "cheat").mkdir()
    (task / "environment").mkdir()
    (task / "instruction.md").write_text("do it")
    (task / "tests/test_a.py").write_text("assert True")
    (task / "cheat/secret.txt").write_text("never read")
    (task / "environment/blob").write_bytes(b"\xff\xfe")
    files, skipped, digests = run.read_package(task)
    assert set(files) == {"instruction.md", "tests/test_a.py"}
    assert skipped == {"task.toml", "solution", "environment"}
    assert "cheat/secret.txt" not in digests and "environment/blob" in digests


def test_default_holdout_never_opens_external_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, _, _ = _manifest(tmp_path)
    monkeypatch.setattr(run, "measure_tb4", lambda *args, **kwargs: ({"status": "measured"}, []))

    def forbidden_external_read(*args: Any, **kwargs: Any) -> None:
        pytest.fail("TB4 DEV-only evaluation attempted to open an external answer key")

    monkeypatch.setattr(run, "measure_key", forbidden_external_read)
    cohorts, _ = run.measure_stage(
        "holdout", manifest=manifest, root=tmp_path, label_map={}, keys_dir=tmp_path
    )
    assert all(
        cohorts[name]["status"] == "unavailable" and "not requested" in cohorts[name]["reason"]
        for name in run.EXTERNAL_COHORT_IDS
    )


# ------------------------------------------------------------------------- immutability


def test_outputs_are_write_once(tmp_path: Path) -> None:
    target = tmp_path / "results-tune-abc.json"
    run.write_new(target, "one")
    with pytest.raises(FileExistsError):
        run.write_new(target, "two")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        run.refuse_existing([target])
    assert target.read_text() == "one"


def _frozen_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    manifest, _, _ = _manifest(tmp_path)
    combined = "c" * 64
    monkeypatch.setattr(run, "load_manifest", lambda *a, **k: manifest)
    monkeypatch.setattr(run, "capabilities", lambda: {"ready": True, "missing": []})
    monkeypatch.setattr(run, "fingerprints", lambda **k: {"combined": combined})
    return {"manifest": manifest, "combined": combined}


def test_holdout_refuses_without_freeze_wrong_identity_or_a_second_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _frozen_env(tmp_path, monkeypatch)
    freeze_path = tmp_path / "freeze.json"
    combined = env["combined"]
    with pytest.raises(ValueError, match="expect-fingerprint"):
        run.run_holdout("", tmp_path, tmp_path, freeze_path=freeze_path)
    with pytest.raises(ValueError, match="no freeze record"):
        run.run_holdout(combined, tmp_path, tmp_path, freeze_path=freeze_path)

    tune_file = tmp_path / "results-tune-ccc.json"
    tune_file.write_text("{}")
    freeze = {
        "schema": run.FREEZE_SCHEMA,
        "fingerprint": {"combined": combined},
        "manifest_sha256": env["manifest"]["manifest_sha256"],
        "tune_results_file": tune_file.name,
        "tune_results_sha256": run.file_sha(tune_file),
    }
    freeze_path.write_text(json.dumps(freeze))
    with pytest.raises(ValueError, match="expected"):
        run.run_holdout("d" * 64, tmp_path, tmp_path, freeze_path=freeze_path)

    tune_file.write_text('{"edited": true}')
    with pytest.raises(ValueError, match="tune result"):
        run.run_holdout(combined, tmp_path, tmp_path, freeze_path=freeze_path)

    tune_file.write_text("{}")
    (tmp_path / "results-holdout-old.json").write_text("{}")
    with pytest.raises(FileExistsError, match="already opened"):
        run.run_holdout(combined, tmp_path, tmp_path, freeze_path=freeze_path)


def test_freeze_refuses_wrong_fingerprint_foreign_tune_result_and_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _frozen_env(tmp_path, monkeypatch)
    combined = env["combined"]
    tune_file = tmp_path / "results-tune-ccc.json"
    tune_file.write_text(
        json.dumps(
            {
                "schema": run.SCHEMA,
                "stage": "tune",
                "fingerprint": {"combined": combined},
                "manifest": {"sha256": env["manifest"]["manifest_sha256"]},
            }
        )
    )
    freeze_path = tmp_path / "freeze.json"
    with pytest.raises(ValueError, match="not the frozen identity"):
        run.run_freeze(tune_file, "d" * 64, freeze_path=freeze_path)
    record = run.run_freeze(tune_file, combined, freeze_path=freeze_path)
    assert record["holdout_opened"] is False and record["tune_results_sha256"] == run.file_sha(
        tune_file
    )
    with pytest.raises(FileExistsError):
        run.run_freeze(tune_file, combined, freeze_path=freeze_path)

    tune_file.write_text(
        json.dumps(
            {
                "schema": run.SCHEMA,
                "stage": "tune",
                "fingerprint": {"combined": "e" * 64},
                "manifest": {"sha256": ""},
            }
        )
    )
    with pytest.raises(ValueError, match="different fingerprint"):
        run.run_freeze(tune_file, combined, freeze_path=tmp_path / "other.json")


# ----------------------------------------------------- registry lock and package binding


class _Item:
    def __init__(self, split: str, item_id: str | None = "i") -> None:
        self.split, self.item_id = split, item_id


def test_external_test_splits_need_the_frozen_eval_token_and_dev_does_not() -> None:
    assert run.external_admission("cheatbench-actions-v1", [_Item("dev")], None) is None
    reason = run.external_admission("cheatbench-actions-v1", [_Item("dev"), _Item("test")], None)
    assert reason and run.TOKEN_ENV in reason and "never bypassed" in reason
    assert run.external_admission("mimo-fresh-window3", [_Item("heldout")], "") is not None


def test_stage_index_collects_only_admitted_complete_lock_digests(tmp_path: Path) -> None:
    def row(trial: str, digest: str, admission: str, state: str) -> str:
        return json.dumps(
            {
                "task": "terminal-bench/t",
                "row_id": "r",
                "hub_trial_id": trial,
                "archive_path": f"/a/{trial}",
                "lock_task": {"source": "p", "digest": digest},
                "provenance_admission": admission,
                "acquisition_state": state,
            }
        )

    path = tmp_path / "trajectory-index.jsonl"
    path.write_text(
        "\n".join(
            [
                row("a", "sha256:good", "admitted_exact_source", "complete"),
                row("b", "sha256:stale", "quarantined", "complete"),
                row("c", "sha256:partial", "admitted_exact_source", "incomplete"),
                '{"task": "terminal-bench/sealed-task", not json',
            ]
        )
        + "\n"
    )
    index, _, locks = run.read_stage_index(path, frozenset({"t"}))
    assert sorted(index) == ["r:t:a", "r:t:b", "r:t:c"]
    assert locks == {"t": frozenset({"sha256:good"})}


def test_package_context_requires_the_exact_hub_lock(tmp_path: Path) -> None:
    root = tmp_path / "packages"
    task = root / "t"
    task.mkdir(parents=True)
    (task / "instruction.md").write_text("do the thing")
    local = run.harbor_task_digest(task)
    contexts, report = run.package_contexts(root, ["t", "gone"], {"t": frozenset({"sha256:other"})})
    assert contexts == {}
    assert (
        report["t"]["reason"] == "package_digest_mismatch"
        and report["t"]["local_harbor_digest"] == local
    )
    assert report["gone"] == {"status": "absent"}
    _, no_lock = run.package_contexts(root, ["t"], None)
    assert "no admitted lock" in no_lock["t"]["reason"]
    _, mixed = run.package_contexts(root, ["t"], {"t": frozenset({local, "sha256:other"})})
    assert mixed["t"]["reason"] == "package_digest_mismatch"  # mixed historical versions never bind
