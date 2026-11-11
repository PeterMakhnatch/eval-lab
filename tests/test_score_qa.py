"""Task versions, grader versions, missingness, and native-cell units stay distinct."""
import json
from types import SimpleNamespace

import pytest

from evallab.score import qa, qa_inputs
from evallab.score.inputs import digest


def test_qa_rates_keep_missing_positives_and_unresolved_negatives():
    subjects = {k: {"family": family} for k, family in (("p1", "a"), ("p2", "a"), ("p3", "b"), ("n1", "c"), ("n2", "d"))}
    members = {"p1": "positive", "p2": "positive", "p3": "positive", "n1": "strong_negative", "n2": "weak_negative"}
    result = qa.measure(members, subjects, {"p1": {"flag": True}, "n1": {"flag": False}}, draws=100, seed=3)
    assert result["positive"]["k"] == 1
    assert result["positive"]["n"] == 3
    assert result["positive"]["macro_per_family"] == .25
    assert result["positive"]["families"] == 2
    assert result["strong_negative"]["k"] == 0
    assert result["weak_negative"]["missing"] == 1
    assert result["weak_negative"]["worst_case_alarm"]["rate"] == 1
    absent = qa.measure(members, subjects, {}, draws=100, seed=3)
    assert absent["positive"]["k"] is None
    assert absent["coverage"]["missing"] == 5


def test_submission_rejection_uses_native_cells_not_graders():
    cells = [
        {"task": "1", "arm": "train", "static_category": "DEFECT", "status": "executed", "reward": 0},
        {"task": "1", "arm": "eval", "static_category": "DEFECT", "status": "executed", "reward": 0},
        {"task": "2", "arm": "eval", "static_category": "DEFECT", "status": "infra_failed", "reward": 0},
        {"task": "2", "arm": "eval", "static_category": "DEFECT", "status": "executed", "reward": .5},
        {"task": "1", "arm": "train", "static_category": "POSSIBLY_VALID", "status": "executed", "reward": 1},
        {"task": "1", "arm": "alternative", "status": "executed", "reward": 0},
        {"task": "1", "arm": "historical_informed_alternative", "status": "executed", "reward": 1},
    ]
    rows = {r["category"]: r for r in qa.submissions(cells, draws=100, seed=3)}
    defect = rows["DEFECT"]
    assert (defect["rejection"]["k"], defect["rejection"]["n"], defect["missing"]) == (2, 2, 2)
    assert defect["rejection"]["families"] == 1
    assert rows["alternative"]["rejection"]["k"] == 1
    assert rows["historical_informed_alternative"]["rejection"]["k"] == 0
    assert rows["POSSIBLY_VALID"]["rejection"]["n"] == 1


@pytest.fixture
def native_join(tmp_path, monkeypatch):
    def ref(name, value):
        path = tmp_path / name
        path.write_text(json.dumps(value))
        return {"path": str(path), "sha256": digest(path)}

    task = "format-code-task-000001"
    generated = {"task_id": task, "split": "dev", "mutated_file": "selected.py",
                 "variant_manifest_sha256": {"original": "original-package", "conflicting_certified_bool": "strict-package"},
                 "variant_test_sha256": {"original": {"selected.py": "original-test"},
                                         "conflicting_certified_bool": {"selected.py": "strict-test"}}}
    controls = {"verified": True, "control_eligible": True, "receipts": {
        "original/oracle": {"docker_rc": 0, "rc": 0}, "original/nop": {"docker_rc": 0, "rc": 1},
        "conflicting_certified_bool/oracle": {"docker_rc": 0, "rc": 1}}}
    monkeypatch.setattr(qa_inputs, "retained_metadata", lambda *_: {
        "/native/generated.json": generated, "/native/controls.json": controls,
        "/native/meta.json": {"split": "dev", "package": "/package"}})
    cells = [{"task": "000001", "arm": arm, "cell_key": arm, "reward": reward,
              "status": "executed", "est_usd": .01, "wall_s": 1}
             for arm, reward in (("reference", 1), ("nop", 0))]
    config = {
        "construction": ref("construction.json", {"results": [{"task_id": task, "admitted": True}]}),
        "originals": ref("originals.json", [{"task_id": task, "mutants_original": {"killed": 0, "total": 2}}]),
        "strict_gold": ref("strict.json", {"tasks": {"000001": {"package_digest": "repaired-package",
            "contract_sha256": "contract", "certified_strict": True, "gates": {"G1": True}}},
            "executed_rows": cells, "reused_rows": []}),
        "strict_selected": ["000001", "000002"], "gold_gates": ["G1"],
        "tools": {name: {} for name in ("oracle", "nop", "mutation_audit", "gold_strict", "gold_G1")},
        "gold_outcomes": ref("outcomes.json", {"runs": {"unknown": {"reward": None}, "passed": {"reward": 1}}}),
    }
    gold = {}
    for key, variant, label in (("strict1", "conflicting_certified_bool", "executed-positive"),
                                ("strict2", "conflicting_certified_bool", "executed-positive"),
                                ("honest", "original", "reviewed-control")):
        gold[key] = SimpleNamespace(source="impossible-task-final", split="DEV", declared_split="dev", task_id=task,
                                   label_state=label, task_available=True, grader_available=True, task_ref="/package", grader_ref="/grader",
                                   metadata={"variant": variant, "source_variant_sha256": generated["variant_test_sha256"][variant]["selected.py"], "G1_pass": True})
    for key in ("unknown", "passed"):
        gold[key] = SimpleNamespace(source="goodfire-action-v1", split="DEV", declared_split="dev", task_id=key,
                                   label_state="executed-positive", task_ref="cas://" + key, grader_digest=None,
                                   provenance=SimpleNamespace(ref="labels.jsonl"))
    return config, gold, tmp_path


def test_passed_action_join_deduplicates_versions_without_inventing_graders(native_join):
    config, gold, root = native_join
    data = qa_inputs.load(config, root, gold)
    assert len(data["panels"]["strict_pairs"]["members"]) == 2
    assert list(data["panels"]["gold_tasks"]["members"].values()).count("positive") == 1
    assert list(data["panels"]["gold_task_bundles"]["members"].values()).count("positive") == 1
    assert list(data["panels"]["gold_tasks"]["members"].values()).count("weak_negative") == 1
    assert len(data["panels"]["gold_graders"]["members"]) == 1
    assert len(data["links"]) == 4
    assert data["unknown_pass_runs"] == 1
    assert data["unbound_passed_graders"] == 1
    assert len(data["panels"]["certification_coverage"]["members"]) == 2
    strict_keys = {r["task_key"] for r in data["links"] if r["source"] == "impossible-task-final"}
    assert len(strict_keys) == 1
    assert strict_keys.isdisjoint(data["panels"]["selected_originals"]["members"])
    assert strict_keys.isdisjoint(data["panels"]["certified_graders"]["members"])


def test_new_offline_protocol_does_not_expand_frozen_qa_subjects(native_join):
    config, gold, root = native_join
    expected = qa_inputs.load(config, root, gold)
    gold["new-protocol"] = SimpleNamespace(
        source="goodfire-action-v2", split="DEV", declared_split="dev",
        task_id="passed", label_state="executed-positive",
    )
    assert qa_inputs.load(config, root, gold) == expected


def test_gold_join_refuses_version_drift_and_protected_rows(native_join):
    config, gold, root = native_join
    gold["strict1"].metadata["source_variant_sha256"] = "different-test"
    with pytest.raises(ValueError, match="constructed grader version"):
        qa_inputs.load(config, root, gold)
    gold.pop("strict1")
    gold["strict2"].split = "TEST"
    with pytest.raises(ValueError, match="DEV only"):
        qa_inputs.load(config, root, gold)


def test_paired_discrimination_can_be_negative():
    result = qa.counted([("a", -1, 1), ("b", 0, 1)], draws=100, seed=3, difference=True)
    assert result["rate"] == -.5
    assert result["task_ci95"]["low"] == -1
    assert result["task_ci95"]["high"] == 0
    assert "wilson_ci95" not in result


def test_findings_require_exact_version_and_verified_evidence(native_join):
    config, gold, root = native_join
    config["tools"].update(qa_task_flagged={}, qa_task_verified={})
    finding = {"subject": {"task_id": gold["strict1"].task_id, "task_digest": "strict-package"},
               "verdict": "fail", "measurement": "measured", "refutation": {"status": "unverified"},
               "reviewer": {"cost_usd": .02}}
    path = root / "finding.json"
    path.write_text(json.dumps(finding))
    binding = {"source": {"path": str(path), "sha256": digest(path)}, "split": "DEV",
               "boundary": "impmore-safe-postprocess-v1", "tool": "qa_task_flagged"}
    config["findings"] = [binding, {**binding, "tool": "qa_task_verified"}]
    data = qa_inputs.load(config, root, gold)
    strict_key = next(r["task_key"] for r in data["links"] if r["run_key"] == "strict1")
    assert data["predictions"]["qa_task_flagged"][strict_key]["flag"] is True
    assert data["predictions"]["qa_task_verified"][strict_key]["flag"] is None
    config["findings"] = [{**binding, "boundary": "different-runtime"}]
    assert not qa_inputs.load(config, root, gold)["predictions"]["qa_task_flagged"]
    config["findings"] = [{**binding, "split": "TEST", "source": {"path": "/must-not-open"}}]
    with pytest.raises(ValueError, match="not DEV"):
        qa_inputs.load(config, root, gold)
