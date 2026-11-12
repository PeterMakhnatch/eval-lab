"""Stored MiMo routing and evidence boundaries, without execution."""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from evallab import audit_mimo
from evallab.dataset_audit_contracts import (
    ALL_STAGES,
    AuditObservation,
    AuditSource,
    AuditStage,
    StageStatus,
)
from evallab.registry import harbor_task_digest, task_directory_digest

TASK = "format-code-task-000792"
OTHER = "format-code-task-000788"
PACKAGE = "sha256:" + "a" * 64
VARIANT = "sha256:" + "b" * 64
HARBOR = "sha256:" + "c" * 64
LEDGER_COLUMNS = [
    "task_id", "split", "status", "reason", "run", "run_digest",
    "run_variant_status", "verdict", "verdict_evidence", "leak_channel",
    "census_label", "census_nop_job", "census_evidence",
]


def _csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _ledger(root: Path, **overrides: str) -> None:
    row = {
        "task_id": TASK, "split": "heldout", "status": "usable", "reason": "nop sound",
        "run": "original", "run_digest": PACKAGE, "run_variant_status": "",
        "verdict": "keep", "verdict_evidence": "stored strip candidate",
        "leak_channel": "unknown", "census_label": "sound", "census_nop_job": "nop-792",
        "census_evidence": "retained census label",
    }
    row.update(overrides)
    _csv(root / audit_mimo.LEDGER, LEDGER_COLUMNS, [row])


def _candidate(root: Path, *, task_name: str | None = None) -> Path:
    path = root / f"library/task-variants/mimo-v2.6-rl__{TASK}/bbbbbbbbbbbb.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": "evallab.task_variant/v1",
        "task_name": task_name or f"mimo-v2.6-rl/{TASK}",
        "variant_digest": VARIANT, "variant_harbor_digest": HARBOR,
        "parent": {
            "digest": PACKAGE, "harbor_digest": HARBOR,
            "source": {"kind": "hf", "repo": audit_mimo.HF_SOURCE["repo"],
                       "revision": audit_mimo.HF_SOURCE["revision"], "path": f"tasks/{TASK}"},
        },
        "transform": "strip-future-history@1", "components_changed": ["environment"],
        "files": [{"path": "environment/setup/setup.sh", "before_sha256": PACKAGE,
                   "after_sha256": VARIANT, "content": "# stripped\n"}],
        "rationale": "strip future history", "inputs": {"leak_patterns": ["unreachable-commits"]},
        "created_by": "har177-default-strip", "created_at": "2026-10-06T22:21:28Z",
        "status": "candidate", "evidence": [],
    }), encoding="utf-8")
    return path


@pytest.mark.parametrize("selector", ["mimo", "mimo-v2.6-rl", audit_mimo.HF_SOURCE["repo"],
                                     f"hf://{audit_mimo.HF_SOURCE['repo']}@{audit_mimo.HF_SOURCE['revision']}"])
def test_explicit_dataset_selectors(selector: str) -> None:
    assert audit_mimo.recognizes_dataset(selector)


@pytest.mark.parametrize("selector", ["hello-world@1.0", "other/mimo-v2.6-rl", "mimo-v2.6-rl-extra",
                                     audit_mimo.HF_SOURCE["repo"] + "@main"])
def test_other_datasets_do_not_inherit_mimo_policy(selector: str) -> None:
    assert not audit_mimo.recognizes_dataset(selector)


@pytest.mark.parametrize("name", [TASK, f"mimo-v2.6-rl/{TASK}", f"mimo-v2.6-rl__{TASK}"])
def test_exact_task_identity_and_numeric_pages(name: str) -> None:
    assert audit_mimo.recognizes_task(name)
    assert audit_mimo.normalize_task_id(name) == TASK
    assert audit_mimo.task_aliases(name) == [TASK, f"mimo-v2.6-rl/{TASK}", f"mimo-v2.6-rl__{TASK}"]
    assert audit_mimo.task_page_name(name) == "task-000792"


@pytest.mark.parametrize("name", [f"unrelated/{TASK}", f"unrelated__{TASK}", f"prefix/{TASK}",
                                 "format-code-task-792", "event-summary", f"mimo-v2.6-rl/{TASK}/x"])
def test_no_unscoped_basename_identity(name: str) -> None:
    assert not audit_mimo.recognizes_task(name)
    with pytest.raises(ValueError):
        audit_mimo.normalize_task_id(name)


def test_isolated_stored_routing_without_process_or_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _csv(tmp_path / audit_mimo.LEDGER, LEDGER_COLUMNS, [
        {
            "task_id": task_id,
            "split": "train",
            "status": status,
            "reason": "stored routing decision",
            "run": "original",
            "run_digest": PACKAGE,
            "run_variant_status": "",
            "verdict": verdict,
            "verdict_evidence": "fixture ledger evidence",
            "leak_channel": "unknown",
        }
        for task_id, verdict, status in (
            (TASK, "keep", "usable"),
            (OTHER, "fix", "review"),
            ("format-code-task-001198", "discard", "discarded"),
        )
    ])
    ledger_path = tmp_path / audit_mimo.LEDGER
    ledger_hash = "sha256:" + hashlib.sha256(ledger_path.read_bytes()).hexdigest()

    def forbidden(*args, **kwargs):
        raise AssertionError("stored collection must not execute or write")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    dataset, records = audit_mimo.read_stored_audit(tmp_path)
    assert dataset.dataset_id == "mimo-v2.6-rl"
    assert len(dataset.tasks) == len(records) == 3
    assert Counter(record.verdict for record in records) == {"keep": 1, "fix": 1, "discard": 1}
    source = next(source for source in dataset.sources if source.path == str(ledger_path))
    assert source.sha256 == ledger_hash
    for record in records:
        assert record.task.path is None
        assert record.task.package_digest == PACKAGE
        assert record.task.harbor_digest is None
        assert record.stages["leak"].status == "recorded"
        assert record.stages["leak"].facts["leak"]["found"] is None
        for stage in ("oracle", "nop", "static", "history", "exploit"):
            assert record.stages[stage].status == "unavailable"
            assert record.stages[stage].reason is not None


def test_legacy_facet_matches_dataset_projection_and_candidate_stays_candidate(tmp_path: Path) -> None:
    _ledger(tmp_path)
    _candidate(tmp_path)
    dataset, records = audit_mimo.read_stored_audit(tmp_path)
    record = records[0]
    assert record.facets == audit_mimo.task_evidence(TASK, repo_root=tmp_path)
    assert record.verdict == "keep"
    assert record.facets["repair"]["status"] == "candidate"
    assert record.task.path is None
    assert record.task.package_digest == PACKAGE
    assert record.task.harbor_digest == HARBOR
    assert record.task.task_name == f"mimo-v2.6-rl/{TASK}"
    assert record.task.parent_source["split"] == "heldout"
    assert dataset.license is None
    assert record.stages["leak"].facts["leak"]["found"] is None
    assert record.stages["oracle"].status == "unavailable"
    assert record.stages["static"].status == "unavailable"
    assert record.stages["history"].facts["solve"]["tag"] == "solve:unknown"
    assert record.stages["exploit"].status == "unavailable"


def test_namespaced_dossier_selector_has_identical_facet(tmp_path: Path) -> None:
    _ledger(tmp_path)
    assert audit_mimo.task_evidence(f"mimo-v2.6-rl/{TASK}", repo_root=tmp_path) == audit_mimo.task_evidence(TASK, repo_root=tmp_path)


def test_other_namespace_lineage_is_not_current_evidence(tmp_path: Path) -> None:
    _ledger(tmp_path)
    _candidate(tmp_path, task_name=f"unrelated/{TASK}")
    _, records = audit_mimo.read_stored_audit(tmp_path)
    assert records[0].facets["repair"] is None
    assert records[0].task.harbor_digest is None
    assert records[0].facets["errors"]


def test_oracle_sweep_precedence_and_mismatch_do_not_rewrite_ledger(tmp_path: Path) -> None:
    _ledger(tmp_path)
    columns = ["task_id", "label", "run_digest", "evidence"]
    _csv(tmp_path / audit_mimo.ORACLE_PILOT, columns,
         [{"task_id": TASK, "label": "oracle:none", "run_digest": PACKAGE, "evidence": "pilot receipt"}])
    _csv(tmp_path / audit_mimo.ORACLE_SWEEP, columns,
         [{"task_id": TASK, "label": "oracle:fail", "run_digest": VARIANT, "evidence": "sweep receipt"}])
    _, records = audit_mimo.read_stored_audit(tmp_path, stages=("oracle",))
    record = records[0]
    assert record.verdict == "keep"
    assert set(record.stages) == {"oracle"}
    oracle = record.stages["oracle"]
    assert [label["label"] for label in oracle.facts["labels"]] == ["oracle:none", "oracle:fail"]
    assert oracle.facts["selected"]["source"] == "oracle_sweep"
    assert oracle.facts["binding"]["digest_match"] is False


def test_static_stale_labels_are_not_routing_or_current_package_truth(tmp_path: Path) -> None:
    _ledger(tmp_path)
    static = tmp_path / "static.csv"
    _csv(static, ["task_id", "health", "solve", "ledger_status", *audit_mimo._STATIC_FLAG_COLS],
         [{"task_id": TASK, "health": "health:cracked", "solve": "solve:always", "ledger_status": "discarded",
           **{column: "1" for column in audit_mimo._STATIC_FLAG_COLS}}])
    _, records = audit_mimo.read_stored_audit(tmp_path, static_audit=static)
    record = records[0]
    assert record.verdict == "keep"
    static_stage = record.stages["static"]
    assert static_stage.status == "recorded"
    assert static_stage.facts["binding"]["digest_match"] is None
    assert static_stage.facts["static_flags"]["ignored_stale"]["ledger_status"] == "discarded"
    assert "health:cracked" not in record.tags


def test_malformed_static_and_history_fail_without_changing_stored_verdict(tmp_path: Path) -> None:
    _ledger(tmp_path)
    static = tmp_path / "static.csv"
    _csv(static, ["task_id", "a_unstated_literal"], [{"task_id": TASK, "a_unstated_literal": "maybe"}])
    _csv(tmp_path / audit_mimo.HISTORY, ["task_id", "runs", "clean_pass", "copied_pass", "fail", "infra"],
         [{"task_id": TASK, "runs": "2", "clean_pass": "0", "copied_pass": "0", "fail": "0", "infra": "0"}])
    _, records = audit_mimo.read_stored_audit(tmp_path, static_audit=static)
    record = records[0]
    assert record.verdict == "keep"
    assert record.stages["static"].status == "failed"
    assert record.stages["history"].status == "failed"
    assert record.stages["history"].facts["solve"]["tag"] == "solve:unknown"


def test_actual_package_uses_distinct_pins_and_rejects_stale_bytes(tmp_path: Path) -> None:
    task = tmp_path / audit_mimo.SNAPSHOT / "tasks" / TASK
    task.mkdir(parents=True)
    (task / "task.toml").write_text(f'[task]\nname = "mimo-v2.6-rl/{TASK}"\n', encoding="utf-8")
    (task / "instruction.md").write_text("Fix the task.\n", encoding="utf-8")
    package = task_directory_digest(task)
    harbor = harbor_task_digest(task)
    assert package != harbor
    _ledger(tmp_path, run_digest=package)
    provenance = task.parents[1] / "provenance.json"
    provenance.write_text(json.dumps({"license": "stored upstream license", "revision": audit_mimo.HF_SOURCE["revision"]}), encoding="utf-8")
    dataset, records = audit_mimo.read_stored_audit(tmp_path)
    selected = records[0].task
    assert dataset.license == "stored upstream license"
    assert selected.path == task.resolve()
    assert selected.package_digest == package
    assert selected.harbor_digest == harbor
    (task / "instruction.md").write_text("Different task bytes.\n", encoding="utf-8")
    _, records = audit_mimo.read_stored_audit(tmp_path)
    assert records[0].task.path is None
    assert records[0].task.package_digest == package
    assert records[0].task.parent_source["package_binding"] == "local package digest mismatch"
    assert records[0].verdict == "keep"


def test_missing_or_invalid_ledger_is_not_an_empty_success(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        audit_mimo.read_stored_audit(tmp_path)
    _csv(tmp_path / audit_mimo.LEDGER, ["task_id"], [{"task_id": TASK}])
    with pytest.raises(ValueError):
        audit_mimo.read_stored_audit(tmp_path)


def test_stage_selection_does_not_execute_or_fabricate_stages(tmp_path: Path) -> None:
    _ledger(tmp_path)
    _, records = audit_mimo.read_stored_audit(tmp_path, stages=())
    assert records[0].stages == {}
    _, records = audit_mimo.read_stored_audit(tmp_path, stages=ALL_STAGES)
    assert set(records[0].stages) == set(ALL_STAGES)
    with pytest.raises(ValueError):
        audit_mimo.read_stored_audit(tmp_path, stages=("unknown",))


@pytest.mark.parametrize(("stage", "findings"), [
    ("nop", {"reward": 1.0, "label": "grader_suspect"}),
    ("nop", {"label": "nop:pass"}),
    ("oracle", {"label": "nop:pass"}),
    ("oracle", {"label": "oracle:none"}),
    ("oracle", {"label": "oracle:fail-network"}),
])
def test_fresh_bound_controls_update_route_without_validating_candidate(
    tmp_path: Path, stage: AuditStage, findings: dict
) -> None:
    _ledger(tmp_path)
    _candidate(tmp_path)
    _, records = audit_mimo.read_stored_audit(tmp_path)
    stored = records[0]
    source = AuditSource(path=str(tmp_path / "control-result.json"), sha256=VARIANT)
    observation = AuditObservation(
        stage=stage,
        status="executed",
        facts={"completed": True, "package_digest": PACKAGE, "harbor_digest": HARBOR, **findings},
        sources=(source,),
    )
    with_control = stored.model_copy(update={"stages": {stage: observation}})
    routed = audit_mimo.finalize_record(with_control)
    assert routed.verdict == "fix"
    assert routed.verdict_reason is not None
    assert routed.verdict_reason != stored.verdict_reason
    assert set(routed.verdict_sources) == {*stored.verdict_sources, source}
    assert "verdict:fix" in routed.tags
    assert "verdict:keep" not in routed.tags
    assert routed.facets["verdict"]["verdict"] == "fix"
    assert routed.facets["verdict"]["evidence"] == routed.verdict_reason
    assert routed.facets["tags"] == list(routed.tags)
    assert routed.facets["repair"]["status"] == "candidate"
    assert routed.facets["health"] == stored.facets["health"]
    assert stored.verdict == "keep"
    assert stored.facets["verdict"]["verdict"] == "keep"
    assert audit_mimo.finalize_record(routed) == routed


@pytest.mark.parametrize(("stage", "status", "findings"), [
    ("oracle", "executed", {"label": "oracle:fail"}),
    ("oracle", "executed", {"label": "oracle:patch-conflict"}),
    ("oracle", "executed", {"label": "oracle:pass+nop:fail"}),
    ("nop", "executed", {"label": "sound", "reward": 0.0}),
    ("nop", "executed", {"label": "grader_suspect", "reward": True}),
    ("static", "executed", {"label": "nop:pass", "reward": 1.0}),
    ("oracle", "recorded", {"label": "oracle:none"}),
    ("oracle", "failed", {"label": "oracle:fail-network"}),
    ("oracle", "unavailable", {"label": "oracle:none"}),
    ("oracle", "executed", {"label": "oracle:none", "completed": False}),
    ("oracle", "executed", {"label": "oracle:none", "completed": None}),
    ("oracle", "executed", {"label": "oracle:none", "package_digest": None}),
    ("oracle", "executed", {"label": "oracle:none", "package_digest": VARIANT}),
    ("oracle", "executed", {"label": "oracle:none", "harbor_digest": VARIANT}),
    ("oracle", "executed", {"label": "oracle:none", "exception": {"type": "RuntimeError"}}),
])
def test_neutral_unbound_or_incomplete_controls_preserve_stored_routing(
    tmp_path: Path, stage: AuditStage, status: StageStatus, findings: dict
) -> None:
    _ledger(tmp_path)
    _candidate(tmp_path)
    _, records = audit_mimo.read_stored_audit(tmp_path)
    observation = AuditObservation(
        stage=stage,
        status=status,
        facts={"completed": True, "package_digest": PACKAGE, "harbor_digest": HARBOR, **findings},
    )
    record = records[0].model_copy(update={"stages": {stage: observation}})
    assert audit_mimo.finalize_record(record) == record


def test_fresh_fix_finding_cannot_override_discard(tmp_path: Path) -> None:
    _ledger(tmp_path, verdict="discard", status="discarded")
    _, records = audit_mimo.read_stored_audit(tmp_path)
    observation = AuditObservation(
        stage="nop", status="executed",
        facts={"completed": True, "package_digest": PACKAGE, "reward": 1.0},
    )
    record = records[0].model_copy(update={"stages": {"nop": observation}})
    assert audit_mimo.finalize_record(record) == record


def test_mimo_static_uses_added_tests_and_excludes_corpus_harness(tmp_path: Path) -> None:
    package = tmp_path / audit_mimo.SNAPSHOT / "tasks" / TASK
    tests = package / "tests"
    tests.mkdir(parents=True)
    (package / "task.toml").write_text(f'[task]\nname = "mimo-v2.6-rl/{TASK}"\n', encoding="utf-8")
    (package / "instruction.md").write_text("Make the answer correct.\n", encoding="utf-8")
    (tests / "test_source.py").write_text("requests.get('https://example.invalid')\n", encoding="utf-8")
    patch = tests / "test.patch"
    patch.write_text(
        "diff --git a/test_answer.py b/test_answer.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/test_answer.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def test_answer():\n"
        "+    assert 1 == 1\n"
        "diff --git a/mimo_test_command.sh b/mimo_test_command.sh\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/mimo_test_command.sh\n"
        "@@ -0,0 +1 @@\n"
        "+requests.get('https://example.invalid')\n",
        encoding="utf-8",
    )
    _ledger(tmp_path, run_digest=task_directory_digest(package))
    _, records = audit_mimo.read_stored_audit(tmp_path)
    observed = audit_mimo.static_observation(records[0].task)
    assert observed.status == "recorded"
    assert observed.facts["b_network"] is False
    patch_source = next(source for source in observed.sources if source.path == str(patch))
    assert patch_source.sha256 == "sha256:" + hashlib.sha256(patch.read_bytes()).hexdigest()
    assert observed.facts["package_digest_match"] is True
    assert audit_mimo.finalize_record(records[0].model_copy(update={"stages": {"static": observed}})).verdict == "keep"


@pytest.mark.parametrize("missing_path", [None, "missing-package"])
def test_absent_mimo_package_has_unavailable_static_evidence(
    tmp_path: Path, missing_path: str | None
) -> None:
    _ledger(tmp_path)
    _, records = audit_mimo.read_stored_audit(tmp_path)
    task = records[0].task.model_copy(update={
        "path": tmp_path / missing_path if missing_path is not None else None,
    })
    observed = audit_mimo.static_observation(task)
    assert observed.status == "unavailable"
    assert observed.sources == ()
    assert observed.reason is not None
