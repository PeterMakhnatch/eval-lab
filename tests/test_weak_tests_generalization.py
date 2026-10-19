"""Scientific selection and execution routing for the frozen native survey."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
STUDY = REPO / "research/experiments/weak-tests-generalization-20261019"


def load_study():
    spec = importlib.util.spec_from_file_location("weak_tests_study", STUDY / "study.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_frozen_sampling_frame_and_equal_probability_selection() -> None:
    plan = json.loads((STUDY / "cohort.json").read_text())
    tb = plan["tb21"]
    assert tb["frame_n"] == len(tb["frame"]) == 89
    assert len({row["task_id"] for row in tb["frame"]}) == 89
    assert {row["task_id"] for row in tb["selected"]}.isdisjoint(tb["prior_live_campaign"])
    assert Counter(row["selection"] for row in tb["frame"]) == {
        "auditor": 2, "traces_campaign": 20, "not_selected": 67,
    }
    hef = plan["humanevalfix"]
    assert hef["frame_n"] == len(hef["frame"]) == 164
    by_size = sorted(hef["frame"], key=lambda row: (row["base_source_bytes"], row["task_id"]))
    for index, row in enumerate(by_size):
        assert row["stratum"] == f"source_size_q{index // 41 + 1}"
        assert row["selection_rank"] == hashlib.sha256(
            (hef["selection_seed"] + ":" + row["task_id"]).encode()
        ).hexdigest()
    for quartile in range(1, 5):
        group = [row for row in hef["frame"] if row["stratum"] == f"source_size_q{quartile}"]
        assert len(group) == 41
        assert {row["task_id"] for row in group if row["selected"]} == {
            row["task_id"] for row in sorted(group, key=lambda row: row["selection_rank"])[:3]
        }


def test_survey_delegates_every_grade_to_the_existing_method(tmp_path, monkeypatch) -> None:
    study = load_study()
    calls = []

    def probe(package, patch, out, **kwargs):
        calls.append((package, patch, out, kwargs))
        return {"status": "measured", "verdict": "unknown"}

    monkeypatch.setattr(study, "probe_task", probe)
    monkeypatch.setattr(study, "RUNS", tmp_path)
    study.run()
    assert len(calls) == 14
    assert all(patch is None for _, patch, _, _ in calls)
    assert all(kwargs["max_grades"] == 24 and kwargs["mutants"] == 8 for *_, kwargs in calls)
    assert all(kwargs["repo_root"] == REPO and kwargs["targets"] for *_, kwargs in calls)
    assert [kwargs["mutation_seed"] for *_, kwargs in calls[:2]] == ["tb21-weak-tests-v1"] * 2
    assert {kwargs["mutation_seed"] for *_, kwargs in calls[2:]} == {study.SEED}
    completion = json.loads((tmp_path / "completion.json").read_text())
    assert len(completion["results"]) == 14
    assert {row["benchmark"] for row in completion["results"]} == {"tb21", "humanevalfix"}


def test_named_schema_fix_attempt_keeps_the_original_cohort_and_receipts(
    tmp_path, monkeypatch
) -> None:
    study = load_study()
    monkeypatch.setattr(study, "RUNS", tmp_path)
    original = b'{"original_receipt": true}\n'
    (tmp_path / "completion.json").write_bytes(original)
    calls = []
    attempt_root = tmp_path / "attempts/schema-fix"

    def probe(package, patch, out, **kwargs):
        snapshot = json.loads((attempt_root / "attempt.json").read_text())
        assert len(snapshot["selected"]) == 12
        assert snapshot["first_pass_preserved"]
        calls.append((package, out, kwargs))
        return {"status": "measured", "verdict": "unknown"}

    monkeypatch.setattr(study, "probe_task", probe)
    study.run(attempt="schema-fix", benchmark="humanevalfix")
    assert len(calls) == 12
    assert all(out.parent == attempt_root / "humanevalfix" for _, out, _ in calls)
    assert (tmp_path / "completion.json").read_bytes() == original
    snapshot = json.loads((attempt_root / "attempt.json").read_text())
    assert snapshot["cohort_sha256"] == "sha256:" + hashlib.sha256(
        (STUDY / "cohort.json").read_bytes()
    ).hexdigest()
    assert snapshot["limits"]["line_mutants"] == 8
    with pytest.raises(ValueError, match="existing survey attempt"):
        study.run(attempt="schema-fix", benchmark="humanevalfix")
    with pytest.raises(ValueError, match="single nonempty path component"):
        study.run(attempt="../escape")


def load_publisher():
    spec = importlib.util.spec_from_file_location("weak_tests_publish", STUDY / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_wilson_matches_the_existing_mimo_sample100_interval() -> None:
    publish = load_publisher()
    rate = publish.wilson(24, 100)
    assert rate["rate"] == 0.24
    assert [round(bound, 4) for bound in rate["wilson_95_ci"]] == [0.1669, 0.3323]
    assert publish.wilson(0, 0)["wilson_95_ci"] is None
    with pytest.raises(ValueError, match="invalid binomial"):
        publish.wilson(2, 1)


@pytest.mark.parametrize("review_count", [0, 2])
def test_missing_and_duplicate_survivor_reviews_cannot_publish(tmp_path, monkeypatch, review_count) -> None:
    publish = load_publisher()
    monkeypatch.setattr(publish, "HERE", tmp_path)
    publish.dump(tmp_path / "cohort.json", {})
    publish.dump(tmp_path / "inventory.json", {"tasks": [{
        "benchmark": "tb21", "task_id": "sample", "candidates": [{"id": "survivor"}],
    }]})
    review = {"benchmark": "tb21", "task_id": "sample", "candidate_id": "survivor"}
    (tmp_path / "reviews.jsonl").write_text((json.dumps(review) + "\n") * review_count)
    with pytest.raises(ValueError, match="every survivor"):
        publish.build()


def test_unmeasured_tasks_never_become_clean_negatives(tmp_path, monkeypatch) -> None:
    publish = load_publisher()
    monkeypatch.setattr(publish, "HERE", tmp_path)
    frame = [{"task_id": "sample", "package_digest": "sha256:" + "a" * 64}]
    publish.dump(tmp_path / "cohort.json", {
        "tb21": {"frame": frame, "own_sampling": "purposive"},
        "humanevalfix": {"frame": frame, "sampling": "stratified"},
    })
    publish.dump(tmp_path / "inventory.json", {"tasks": []})
    (tmp_path / "reviews.jsonl").write_text("")
    results, atlas, items, findings = publish.build()
    assert not findings
    assert {item["expected_verdict"] for item in items} == {"unknown"}
    assert all(not item["source_visible"] and not item["selected_by"] for item in items)
    assert {row["status"] for row in atlas} == {"not_run"}
    for row in results["benchmarks"].values():
        assert row["unknown_tasks"] == 1
        assert row["clean_negative_tasks"] == row["attempted_n"] == 0
        assert row["confirmed_rate_attempted"]["rate"] is None


def test_operator_inapplicability_is_counted_without_labeling_clean(tmp_path, monkeypatch) -> None:
    publish = load_publisher()
    monkeypatch.setattr(publish, "HERE", tmp_path)
    monkeypatch.setattr(publish, "EVIDENCE", tmp_path)
    frame = [{"task_id": "sample", "package_digest": "sha256:" + "a" * 64}]
    publish.dump(tmp_path / "cohort.json", {
        "tb21": {"frame": frame, "own_sampling": "purposive"},
        "humanevalfix": {"frame": frame, "sampling": "stratified"},
    })
    report = tmp_path / "report.json"
    publish.dump(report, {
        "status": "not_applicable", "package_digest": frame[0]["package_digest"],
        "coverage": {"subset": "not_applicable", "mutations": "not_applicable"},
        "code_blocks": 1, "code_lines": 4, "mutants": {"generated": 0, "tried": 0},
    })
    source = "runs/sample/report.json"
    publish.dump(tmp_path / "inventory.json", {"tasks": [{
        "benchmark": "humanevalfix", "task_id": "sample", "producer": "unit-test",
        "source_report": source,
        "files": {source: {"path": str(report), "sha256": publish.digest(report)}},
        "candidates": [],
    }]})
    (tmp_path / "reviews.jsonl").write_text("")
    results, atlas, items, findings = publish.build()
    search = results["benchmarks"]["humanevalfix"]["primary_search"]
    assert search["subset_coverage_counts"] == {"not_applicable": 1}
    assert search["line_coverage_counts"] == {"not_applicable": 1}
    assert search["line_candidates_generated_recorded"] == search["line_candidates_tried"] == 0
    assert search["line_generation_unreported_tasks"] == 0
    assert not findings
    observed = next(row for row in atlas if row["benchmark"] == "humanevalfix")
    assert observed["reference_code_blocks"] == 1
    assert observed["semantic_verdict"] == "not_established"
    item = next(item for item in items if item["subject"]["task_id"] == "humanevalfix/sample")
    assert item["expected_verdict"] == "unknown" and item["source_visible"]
    assert not item["selected_by"]


@pytest.mark.parametrize("fault", [
    "none", "isolation", "source", "timeout", "model", "script", "receipt",
    "agent_override", "boolean_reward", "unscored", "stdout_bytes", "stdout_missing",
])
def test_native_qualification_requires_real_bound_controls(tmp_path, monkeypatch, fault) -> None:
    publish = load_publisher()
    monkeypatch.setattr(publish, "HERE", tmp_path)
    monkeypatch.setattr(publish, "REPO", tmp_path)
    monkeypatch.setattr(publish, "EVIDENCE", tmp_path)
    package = "sha256:" + "a" * 64
    staged = "sha256:" + "b" * 64
    solution = hashlib.sha256(b"#!/bin/bash\ntrue\n").hexdigest()
    result = {"name": "control", "status": "ok", "rewards": [1.0],
              "staged_task_digest": staged, "solution_sha256": solution}
    content = {
        "script.sh": "#!/bin/bash\ntrue\n",
        "matrix.json": {"task_package_digest": package},
        "receipt.json": {"results": [result | ({"rewards": [0.0]} if fault == "receipt" else {})]},
        "trial/agent/oracle.txt": "",
        "trial/verifier/test-stdout.txt": "1 passed\n",
        "trial/host-isolation.json": {"applied": fault != "isolation", "error_type": None},
        "job/lab-metadata.json": {
            "exit_code": 0, "timed_out": False,
            "task_staging": {"source_package_digest": "wrong" if fault == "source" else staged},
        },
        "trial/result.json": {
            "exception_info": None, "verifier_result": {"rewards": {"reward": 1.0}},
            "agent_execution": {"started_at": "start", "finished_at": "end"},
            "config": {
                "agent": {"name": "oracle", "model_name": "unexpected" if fault == "model" else None},
                "environment": {
                    "import_path": "evallab.harbor_host_isolation:HostIsolatedDockerEnvironment",
                    "override_cpus": None, "override_memory_mb": None,
                    "override_storage_mb": None, "override_gpus": None,
                },
                "timeout_multiplier": 0.1 if fault == "timeout" else 1.0,
                "agent_timeout_multiplier": 1.0, "verifier_timeout_multiplier": None,
                "verifier": {"override_timeout_sec": None},
            },
        },
    }
    files = {}
    for name, value in content.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        files[name] = {"path": name, "sha256": publish.digest(path)}
    native = {
        "script": "script.sh", "script_sha256": "wrong" if fault == "script" else files["script.sh"]["sha256"],
        "matrix": "matrix.json", "trials": ["trial"], "job_dir": "job", "package_digest": package,
        "name": "control", "receipt": "receipt.json", "result": result,
    }
    if fault == "agent_override":
        content["trial/result.json"]["config"]["agent"]["override_timeout_sec"] = 1
    elif fault == "boolean_reward":
        content["trial/result.json"]["verifier_result"]["rewards"]["reward"] = True
    if fault in {"agent_override", "boolean_reward"}:
        path = tmp_path / "trial/result.json"
        path.write_text(json.dumps(content["trial/result.json"]))
        files["trial/result.json"]["sha256"] = publish.digest(path)
    elif fault == "stdout_bytes":
        (tmp_path / "trial/verifier/test-stdout.txt").write_text("changed\n")
    elif fault == "stdout_missing":
        del files["trial/verifier/test-stdout.txt"]
    report = {"package_digest": package, "grades": [{
        "label": "full", "status": "error" if fault == "unscored" else "scored",
        "reward": 1.0, "rc": None, "native": native,
    }]}
    row = {"files": files, "task_id": "sample"}
    if fault == "none":
        assert publish.grade_proof(row, report, "full", 1.0)["reward"] == 1.0
    else:
        with pytest.raises(ValueError, match="native proof|retained evidence|test output"):
            publish.grade_proof(row, report, "full", 1.0)


def test_materialization_uses_the_full_frozen_frame_not_the_paired_key(tmp_path, monkeypatch) -> None:
    study = load_study()
    calls = []
    module = SimpleNamespace(PINS={"tb21": object()})
    module.materialize_packages = lambda items, *args, **kwargs: (
        calls.append(items) or {"mismatches": 0, "unmaterialized": 0}
    )
    loader = SimpleNamespace(exec_module=lambda _: None)
    spec = SimpleNamespace(name="tb21_materialize", loader=loader)
    monkeypatch.setattr(study.importlib.util, "spec_from_file_location", lambda *args: spec)
    monkeypatch.setattr(study.importlib.util, "module_from_spec", lambda _: module)
    monkeypatch.setitem(study.sys.modules, spec.name, module)
    monkeypatch.setattr(study, "RUNS", tmp_path)
    frame = [{"task_id": name, "package_digest": "sha256:" + "a" * 64}
             for name in ("paired-task", "outside-the-paired-key")]
    study.materialize(frame, {row["task_id"] for row in frame})
    assert {item["subject"]["task_id"] for item in calls[0]} == {
        "tb21/paired-task", "tb21/outside-the-paired-key",
    }
    with pytest.raises(ValueError, match="cover every selected"):
        study.materialize(frame[:1], {row["task_id"] for row in frame})


def test_execution_counts_deduplicate_reused_native_controls() -> None:
    publish = load_publisher()
    grades = [
        {"status": "scored", "native": {"trials": ["same-control"]}},
        {"status": "scored", "native": {"trials": ["same-control"]}},
        {"status": "infra", "native": {"trials": ["failed-trial"]}},
        {"status": "infra", "native": None},
    ]
    assert publish._execution_counts({"grades": grades}) == {
        "grade_records": 4, "scored_grades": 2, "native_trials": 2,
    }


@pytest.mark.parametrize("earlier_candidate", [False, True])
def test_collect_preserves_earlier_attempts_and_never_hides_their_candidates(
    tmp_path, monkeypatch, earlier_candidate
) -> None:
    publish = load_publisher()
    here = tmp_path / "experiment"
    runs = tmp_path / "runs/survey"
    evidence = tmp_path / "retained"
    for name, value in (("REPO", tmp_path), ("HERE", here), ("RUNS", runs), ("EVIDENCE", evidence)):
        monkeypatch.setattr(publish, name, value)
    package = tmp_path / "package"
    publish.dump(package / "task.toml", {})
    (package / "instruction.md").write_text("Offline transport fixture; no scientific label.\n")
    selection = {"task_id": "sample", "package": "package", "selected": True}
    plan = {
        "limits": {"subset_grades": 24, "line_mutants": 8},
        "tb21": {"selected": []}, "humanevalfix": {"frame": [selection]},
    }
    publish.dump(here / "cohort.json", plan)
    publish.dump(runs / "completion.json", {"results": []})
    first = {
        "package": str(package), "status": "not_applicable", "grades": [],
    }
    if earlier_candidate:
        first.update(dropped_code=True, grades=[
            {"label": "confirm", "status": "scored", "reward": 1, "native": {}},
        ])
    publish.dump(runs / "humanevalfix/sample/report.json", first)
    retry = runs / "attempts/schema-fix"
    selected = [{"benchmark": "humanevalfix", "task_id": "sample"}]
    publish.dump(retry / "attempt.json", {
        "cohort_sha256": publish.digest(here / "cohort.json"),
        "limits": plan["limits"], "selected": selected,
    })
    publish.dump(retry / "completion.json", {"results": selected})
    publish.dump(retry / "humanevalfix/sample/report.json", {
        "package": str(package), "status": "unavailable", "grades": [],
    })
    if earlier_candidate:
        with pytest.raises(ValueError, match="candidate-bearing attempt"):
            publish.collect(False, ["schema-fix"])
        assert not (here / "inventory.json").exists()
        return
    publish.collect(False, ["schema-fix"])
    bundle = json.loads((here / "inventory.json").read_text())
    row, = bundle["tasks"]
    assert bundle["owned_attempts"] == ["first-pass", "schema-fix"]
    old, = row["prior_attempts"]
    assert json.loads(publish.checked(row, old["source_report"]).read_text()) == first
    assert json.loads(publish.checked(row, row["source_report"]).read_text())["status"] == "unavailable"
    assert not row["candidates"]
