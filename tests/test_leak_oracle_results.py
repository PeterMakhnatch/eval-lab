"""Offline receipt behavior, with real local patch/log/JSON bytes.

These synthetic control receipts exercise classification and provenance boundaries;
they are not paid executions, scientific sweep rows, or model trajectories.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "research/experiments/leak-oracle/results.py"
spec = importlib.util.spec_from_file_location("leak_oracle_results", SCRIPT)
results = importlib.util.module_from_spec(spec)
spec.loader.exec_module(results)

BASE = "a" * 40
FIX = "b" * 40
TIP = "c" * 40
PATCH = (
    b"diff --git a/package/core.py b/package/core.py\n"
    b"--- a/package/core.py\n+++ b/package/core.py\n"
    b"@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n"
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def receipt(tmp_path: Path, number: int = 1) -> dict:
    task_id = f"format-code-task-{number:06d}"
    directory = tmp_path / task_id
    directory.mkdir()
    patch = directory / "solution.patch"
    patch.write_bytes(PATCH)
    value = {
        "schema_version": 1,
        "task_id": task_id,
        "run_digest": "sha256:" + "1" * 64,
        "image": "registry.invalid/task@sha256:" + "2" * 64,
        "base": BASE,
        "test_patch_sha256": "3" * 64,
        "extraction": {
            "status": "ok",
            "fix": {"sha": FIX},
            "strategy": "S1 source path",
            "rationale": "synthetic offline selection",
            "solution_patch_sha256": digest(PATCH),
            "solution_patch_path": str(patch),
            "solution_patch_bytes": len(PATCH),
            "apply_check_on_base": True,
        },
        "arms": {},
    }
    value["arms"]["nop"] = arm(value, directory, "nop", 1)
    value["arms"]["oracle"] = arm(value, directory, "oracle", 0)
    return value


def arm(value: dict, directory: Path, name: str, code: int) -> dict:
    log = directory / f"{name}.log"
    # A completed verifier may be entirely quiet. Its empty log is real evidence.
    log.write_bytes(b"")
    return {
        "status": "complete",
        "sandbox_id": f"sb-{value['task_id']}-{name}",
        **{field: value[field] for field in ("run_digest", "image", "base", "test_patch_sha256")},
        "network_mode": "open" if name == "open_oracle" else "locked",
        "test_patch_applied": True,
        "solution_patch_applied": name != "nop",
        "solution_patch_sha256": value["extraction"]["solution_patch_sha256"]
        if name != "nop"
        else None,
        "verifier_exit_code": code,
        "evidence_path": str(log),
        "evidence_sha256": digest(b""),
    }


def add_open(value: dict, code: int = 0) -> None:
    directory = Path(value["extraction"]["solution_patch_path"]).parent
    value["arms"]["open_oracle"] = arm(value, directory, "open_oracle", code)


def manifest(*values: dict) -> dict:
    return {
        "schema_version": 1,
        "ledger_sha256": "4" * 64,
        "origin": "direct-Docker diagnostics",
        "license": "preserved-test-license",
        "split": "frozen-development",
        "inclusion_rule": "status usable or review",
        "tasks": [
            {
                field: value[field]
                for field in ("task_id", "run_digest", "image", "base", "test_patch_sha256")
            }
            for value in values
        ],
    }


def observation(tmp_path: Path, value: dict, filename: str | None = None) -> tuple[Path, dict]:
    directory = tmp_path / "receipts"
    directory.mkdir(exist_ok=True)
    path = directory / (filename or f"{value['task_id']}.json")
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return path, value


def export(tmp_path: Path, cohort: dict, observations: list) -> tuple[dict, list[dict], dict]:
    csv_path, coverage_path = tmp_path / "sweep.csv", tmp_path / "coverage.json"
    summary = results.write_sweep(cohort, observations, csv_path, coverage_path)
    with csv_path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    return summary, rows, json.loads(coverage_path.read_text())


@pytest.mark.parametrize(
    "label",
    [
        "oracle:pass+nop:fail",
        "oracle:fail-network",
        "oracle:fail",
        "oracle:none",
        "oracle:patch-conflict",
        "nop:pass",
    ],
)
def test_all_six_scientific_labels(tmp_path: Path, label: str) -> None:
    value = receipt(tmp_path)
    if label == "oracle:fail-network":
        value["arms"]["oracle"]["verifier_exit_code"] = 1
        add_open(value)
    elif label == "oracle:fail":
        value["arms"]["oracle"]["verifier_exit_code"] = 1
    elif label == "oracle:none":
        value["extraction"]["status"] = "no-identifiable-fix"
    elif label == "oracle:patch-conflict":
        value["extraction"]["status"] = "patch-no-apply"
    elif label == "nop:pass":
        value["arms"]["nop"]["verifier_exit_code"] = 0
        value["extraction"]["status"] = "git-error"
    verdict = results.classify_receipt(value)
    assert verdict["label"] == label
    assert verdict["state"] == "classified"
    assert verdict["reason"]


@pytest.mark.parametrize(
    "extraction",
    [
        None,
        [],
        {"status": "needs-tip-decision"},
        {"status": "unsupported-tree"},
        {"status": "patch-no-apply"},
        {"status": "test-only-fix"},
    ],
)
def test_quiet_locked_nop_pass_has_precedence(tmp_path: Path, extraction) -> None:
    value = receipt(tmp_path)
    value["arms"]["nop"]["verifier_exit_code"] = 0
    value["extraction"] = extraction
    value["arms"]["oracle"] = {"status": "budget-stopped"}
    assert Path(value["arms"]["nop"]["evidence_path"]).read_bytes() == b""
    assert results.classify_receipt(value)["label"] == "nop:pass"


@pytest.mark.parametrize("status", ["no-identifiable-fix", "test-only-fix", "empty-diff"])
def test_none_does_not_advertise_rejected_candidate(tmp_path: Path, status: str) -> None:
    value = receipt(tmp_path)
    value["extraction"].update({"status": status, "tip": TIP})
    del value["arms"]["oracle"]
    summary, rows, coverage = export(tmp_path, manifest(value), [observation(tmp_path, value)])
    assert summary["classified_count"] == 1
    assert rows[0]["label"] == "oracle:none"
    assert rows[0]["fix_commit"] == ""
    assert rows[0]["patch_tip"] == ""
    assert coverage["tasks"][0]["extraction_status"] == status


def test_divergence_keeps_introducing_fix_separate_from_patch_tip(tmp_path: Path) -> None:
    value = receipt(tmp_path)
    value["extraction"].update({"status": "ok-divergent", "tip": TIP, "strategy": "S2b"})
    source = observation(tmp_path, value)
    _, rows, coverage = export(tmp_path, manifest(value), [source])
    assert rows[0]["fix_commit"] == FIX
    assert rows[0]["patch_tip"] == TIP
    assert rows[0]["how_chosen"] == "S2b"
    assert rows[0]["evidence_path"] == str(source[0])
    assert coverage["tasks"][0]["solution_patch_sha256"] == digest(PATCH)


def assert_arm_problem(value: dict, name: str, state: str) -> dict:
    verdict = results.classify_receipt(value)
    if name == "open_oracle":
        assert verdict["label"] == "oracle:fail"
        assert verdict["state"] == "classified"
        detail = verdict["network_confirmation"]
    else:
        assert verdict["label"] is None
        detail = verdict
    assert detail["state"] == state
    assert detail["reason"]
    return detail


@pytest.mark.parametrize("name", ["nop", "oracle", "open_oracle"])
@pytest.mark.parametrize(
    "field,bad",
    [
        ("run_digest", "sha256:" + "9" * 64),
        ("image", "registry.invalid/stale@sha256:" + "8" * 64),
        ("base", "9" * 40),
        ("test_patch_sha256", "9" * 64),
        ("task_id", "format-code-task-000099"),
    ],
)
def test_each_arm_is_bound_to_exact_task_source(
    tmp_path: Path, name: str, field: str, bad: str
) -> None:
    value = receipt(tmp_path)
    value["arms"]["oracle"]["verifier_exit_code"] = 1
    add_open(value)
    value["arms"][name][field] = bad
    detail = assert_arm_problem(value, name, "source-mismatch")
    assert field in detail["reason"]


@pytest.mark.parametrize(
    "name,mode", [("nop", "open"), ("oracle", "open"), ("open_oracle", "locked")]
)
def test_network_mode_is_explicit_not_inferred(tmp_path: Path, name: str, mode: str) -> None:
    value = receipt(tmp_path)
    value["arms"]["oracle"]["verifier_exit_code"] = 1
    add_open(value)
    value["arms"][name]["network_mode"] = mode
    assert_arm_problem(value, name, "source-mismatch")


@pytest.mark.parametrize("name", ["oracle", "open_oracle"])
def test_confirmation_patch_hash_must_match_extraction(tmp_path: Path, name: str) -> None:
    value = receipt(tmp_path)
    value["arms"]["oracle"]["verifier_exit_code"] = 1
    add_open(value)
    value["arms"][name]["solution_patch_sha256"] = "9" * 64
    assert_arm_problem(value, name, "source-mismatch")


@pytest.mark.parametrize(
    "name,other",
    [
        ("oracle", "nop"),
        ("open_oracle", "nop"),
        ("open_oracle", "oracle"),
    ],
)
def test_each_pair_requires_fresh_sandbox(tmp_path: Path, name: str, other: str) -> None:
    value = receipt(tmp_path)
    value["arms"]["oracle"]["verifier_exit_code"] = 1
    add_open(value)
    value["arms"][name]["sandbox_id"] = value["arms"][other]["sandbox_id"]
    assert_arm_problem(value, name, "source-mismatch")


def test_network_error_text_without_positive_open_pair_is_ordinary_fail(tmp_path: Path) -> None:
    value = receipt(tmp_path)
    failed = value["arms"]["oracle"]
    failed["verifier_exit_code"] = 1
    content = b"ConnectionError: network is unreachable\n"
    Path(failed["evidence_path"]).write_bytes(content)
    failed["evidence_sha256"] = digest(content)
    assert results.classify_receipt(value)["label"] == "oracle:fail"
    add_open(value, 1)
    assert results.classify_receipt(value)["label"] == "oracle:fail"
    value["arms"]["open_oracle"]["verifier_exit_code"] = 0
    assert results.classify_receipt(value)["label"] == "oracle:fail-network"


@pytest.mark.parametrize("name", ["nop", "oracle", "open_oracle"])
@pytest.mark.parametrize("status", ["timeout", "infrastructure-error", "budget-stopped", "not-run"])
def test_operational_arm_outcomes_are_unknown(tmp_path: Path, name: str, status: str) -> None:
    value = receipt(tmp_path)
    value["arms"]["oracle"]["verifier_exit_code"] = 1
    add_open(value)
    value["arms"][name] = {"status": status}
    assert_arm_problem(value, name, status)


@pytest.mark.parametrize("name", ["nop", "oracle"])
def test_missing_required_arm_is_not_a_scientific_failure(tmp_path: Path, name: str) -> None:
    value = receipt(tmp_path)
    del value["arms"][name]
    verdict = results.classify_receipt(value)
    assert verdict["label"] is None
    assert verdict["state"] == "not-run"


@pytest.mark.parametrize(
    "code,state",
    [
        (None, "infrastructure-error"),
        (True, "infrastructure-error"),
        ("1", "infrastructure-error"),
        (-9, "infrastructure-error"),
        (124, "timeout"),
        (137, "infrastructure-error"),
        (143, "infrastructure-error"),
    ],
)
def test_nonzero_is_not_enough_for_a_scientific_failure(tmp_path: Path, code, state: str) -> None:
    value = receipt(tmp_path)
    value["arms"]["oracle"]["verifier_exit_code"] = code
    verdict = results.classify_receipt(value)
    assert verdict["label"] is None
    assert verdict["state"] == state


@pytest.mark.parametrize(
    "field,bad",
    [
        ("timed_out", True),
        ("killed", True),
        ("oom_killed", True),
        ("termination_reason", "SDK-error"),
        ("test_patch_applied", False),
        ("solution_patch_applied", False),
    ],
)
def test_explicit_termination_or_patch_failure_is_unknown(tmp_path: Path, field: str, bad) -> None:
    value = receipt(tmp_path)
    value["arms"]["oracle"].update({"verifier_exit_code": 1, field: bad})
    assert results.classify_receipt(value)["label"] is None


@pytest.mark.parametrize("name", ["nop", "oracle"])
def test_explicit_patch_application_flag_is_required(tmp_path: Path, name: str) -> None:
    value = receipt(tmp_path)
    del value["arms"][name]["solution_patch_applied"]
    assert results.classify_receipt(value)["state"] == "infrastructure-error"


def test_nop_must_not_have_a_solution_patch(tmp_path: Path) -> None:
    value = receipt(tmp_path)
    value["arms"]["nop"]["verifier_exit_code"] = 0
    value["arms"]["nop"]["solution_patch_applied"] = True
    assert results.classify_receipt(value)["label"] is None


@pytest.mark.parametrize(
    "status,state",
    [
        ("needs-tip-decision", "ambiguous"),
        ("unsupported-tree", "infrastructure-error"),
        ("git-error", "infrastructure-error"),
        ("git-timeout", "timeout"),
        ("input-error", "infrastructure-error"),
        ("output-error", "infrastructure-error"),
    ],
)
def test_extraction_operational_outcomes_are_not_none(
    tmp_path: Path, status: str, state: str
) -> None:
    value = receipt(tmp_path)
    value["extraction"]["status"] = status
    verdict = results.classify_receipt(value)
    assert verdict["label"] is None
    assert verdict["state"] == state


@pytest.mark.parametrize(
    "change",
    [
        "missing-path",
        "missing-digest",
        "empty",
        "whitespace",
        "changed-bytes",
        "missing-file",
        "no-apply-check",
    ],
)
def test_retained_solution_bytes_are_required_and_bound(tmp_path: Path, change: str) -> None:
    value = receipt(tmp_path)
    extraction = value["extraction"]
    patch = Path(extraction["solution_patch_path"])
    if change == "missing-path":
        del extraction["solution_patch_path"]
    elif change == "missing-digest":
        del extraction["solution_patch_sha256"]
    elif change in ("empty", "whitespace"):
        content = b"" if change == "empty" else b" \n\t"
        patch.write_bytes(content)
        extraction["solution_patch_sha256"] = digest(content)
        extraction["solution_patch_bytes"] = len(content)
        value["arms"]["oracle"]["solution_patch_sha256"] = digest(content)
    elif change == "changed-bytes":
        patch.write_bytes(PATCH.replace(b"VALUE = 2", b"VALUE = 9"))
    elif change == "missing-file":
        patch.unlink()
    else:
        del extraction["apply_check_on_base"]
    assert results.classify_receipt(value)["label"] is None


@pytest.mark.parametrize("name", ["nop", "oracle", "open_oracle"])
@pytest.mark.parametrize("change", ["missing-hash", "changed-bytes", "missing-file"])
def test_log_bytes_are_bound_even_for_quiet_verifiers(
    tmp_path: Path, name: str, change: str
) -> None:
    value = receipt(tmp_path)
    value["arms"]["oracle"]["verifier_exit_code"] = 1
    add_open(value)
    selected = value["arms"][name]
    if change == "missing-hash":
        del selected["evidence_sha256"]
    elif change == "changed-bytes":
        Path(selected["evidence_path"]).write_bytes(b"replaced log\n")
    else:
        Path(selected["evidence_path"]).unlink()
    state = "source-mismatch" if change == "changed-bytes" else "missing-evidence"
    assert_arm_problem(value, name, state)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("base", "f" * 40),
        ("task", "format-code-task-000999"),
        ("run_digest", "sha256:" + "9" * 64),
    ],
)
def test_extraction_source_binding_cannot_be_stale(tmp_path: Path, field: str, bad: str) -> None:
    value = receipt(tmp_path)
    value["extraction"][field] = bad
    assert results.classify_receipt(value)["state"] == "source-mismatch"


@pytest.mark.parametrize("bad", [None, [], {}, {"schema_version": True}, {"schema_version": 2}])
def test_malformed_standalone_receipts_are_concrete_unknowns(bad) -> None:
    verdict = results.classify_receipt(bad)
    assert verdict["label"] is None
    assert verdict["state"] == "invalid"
    assert verdict["reason"]


@pytest.mark.parametrize(
    "field,bad",
    [
        ("task_id", "../format-code-task-000001"),
        ("task_id", "format-code-task-1"),
        ("run_digest", "sha256:missing"),
        ("base", "HEAD"),
        ("test_patch_sha256", ""),
        ("image", ""),
    ],
)
def test_invalid_source_identity_is_rejected_without_outputs(
    tmp_path: Path, field: str, bad: str
) -> None:
    value = receipt(tmp_path)
    cohort = manifest(value)
    value[field] = bad
    source = observation(tmp_path, value)
    with pytest.raises(ValueError, match=field):
        results.write_sweep(cohort, [source], tmp_path / "sweep.csv", tmp_path / "coverage.json")
    assert not (tmp_path / "sweep.csv").exists()
    assert not (tmp_path / "coverage.json").exists()


@pytest.mark.parametrize(
    "image", ["registry.invalid/task:latest", "registry.invalid/task@sha256:abcd"]
)
def test_mutable_or_incomplete_image_binding_cannot_certify_a_pair(
    tmp_path: Path, image: str
) -> None:
    value = receipt(tmp_path)
    value["image"] = image
    for phase in value["arms"].values():
        phase["image"] = image
    verdict = results.classify_receipt(value)
    assert verdict["label"] is None
    assert verdict["state"] == "invalid"
    with pytest.raises(ValueError, match="image"):
        export(tmp_path, manifest(value), [observation(tmp_path, value)])


@pytest.mark.parametrize(
    "field,bad",
    [
        ("run_digest", "sha256:" + "9" * 64),
        ("image", "registry.invalid/stale@sha256:" + "9" * 64),
        ("base", "f" * 40),
        ("test_patch_sha256", "9" * 64),
    ],
)
def test_receipt_must_match_the_frozen_manifest(tmp_path: Path, field: str, bad: str) -> None:
    value = receipt(tmp_path)
    cohort = manifest(value)
    value[field] = bad
    with pytest.raises(ValueError):
        export(tmp_path, cohort, [observation(tmp_path, value)])
    assert not (tmp_path / "sweep.csv").exists()


@pytest.mark.parametrize("status", ["budget-stopped", "infrastructure-error", "timeout"])
def test_unavailable_base_preserves_operational_coverage(tmp_path: Path, status: str) -> None:
    value = receipt(tmp_path)
    cohort = manifest(value)
    value.update(
        base=None,
        execution_status=status,
        execution_reason="No source observation was available before the stop",
        arms={},
    )
    summary, rows, coverage = export(tmp_path, cohort, [observation(tmp_path, value)])
    assert rows == []
    assert summary["classified_count"] == 0
    assert coverage["tasks"][0]["state"] == status
    assert coverage["tasks"][0]["base"] is None


@pytest.mark.parametrize("conflicting", [False, True])
def test_duplicate_observations_are_never_last_writer_wins(
    tmp_path: Path, conflicting: bool
) -> None:
    value = receipt(tmp_path)
    second = copy.deepcopy(value)
    if conflicting:
        second["arms"]["nop"]["verifier_exit_code"] = 0
    observations = [observation(tmp_path, value), observation(tmp_path, second, "duplicate.json")]
    with pytest.raises(ValueError, match="duplicate receipt"):
        export(tmp_path, manifest(value), observations)
    assert not (tmp_path / "sweep.csv").exists()


def test_duplicate_manifest_tasks_are_rejected(tmp_path: Path) -> None:
    value = receipt(tmp_path)
    with pytest.raises(ValueError, match="duplicate manifest"):
        export(tmp_path, manifest(value, value), [])


def test_foreign_task_and_stale_in_memory_receipt_are_rejected(tmp_path: Path) -> None:
    value = receipt(tmp_path)
    another = receipt(tmp_path, 2)
    with pytest.raises(ValueError, match="absent from manifest"):
        export(tmp_path, manifest(value), [observation(tmp_path, another)])
    source = observation(tmp_path, value)
    value["arms"]["nop"]["verifier_exit_code"] = 0
    with pytest.raises(ValueError, match="differs from source JSON"):
        export(tmp_path, manifest(value), [source])


def test_complete_coverage_retains_unknown_and_unrun_tasks(tmp_path: Path) -> None:
    passed, stopped, ambiguous, unrun = [receipt(tmp_path, number) for number in range(1, 5)]
    stopped["arms"]["oracle"] = {"status": "budget-stopped"}
    ambiguous["extraction"]["status"] = "needs-tip-decision"
    sources = [observation(tmp_path, value) for value in (ambiguous, stopped, passed)]
    cohort = manifest(unrun, ambiguous, stopped, passed)
    cohort["tasks"][0]["license"] = "task-specific-license"
    summary, rows, coverage = export(tmp_path, cohort, sources)
    assert summary["requested_count"] == 4
    assert summary["observed_count"] == 3
    assert summary["classified_count"] == 1
    assert summary["unclassified_count"] == 3
    assert summary["counts_per_label"]["oracle:pass+nop:fail"] == 1
    assert len(rows) == 1
    assert rows[0]["task_id"] == passed["task_id"]
    assert [task["task_id"] for task in coverage["tasks"]] == sorted(
        value["task_id"] for value in (passed, stopped, ambiguous, unrun)
    )
    assert [task["state"] for task in coverage["tasks"]] == [
        "classified",
        "budget-stopped",
        "ambiguous",
        "not-run",
    ]
    assert all(task["label"] is None for task in coverage["tasks"][1:])
    assert coverage["tasks"][3]["receipt_path"] is None
    assert coverage["tasks"][3]["license"] == "task-specific-license"
    assert coverage["manifest_metadata"]["split"] == cohort["split"]
    assert coverage["manifest_metadata"]["ledger_sha256"] == cohort["ledger_sha256"]
    assert coverage["sources"]["receipts"][0]["sha256"] == digest(sources[2][0].read_bytes())
    assert coverage["csv_sha256"] == digest((tmp_path / "sweep.csv").read_bytes())
    first_csv, first_coverage = (
        (tmp_path / "sweep.csv").read_bytes(),
        (tmp_path / "coverage.json").read_bytes(),
    )
    # Receipt traversal order cannot change either artifact's bytes.
    export(tmp_path, cohort, list(reversed(sources)))
    assert (tmp_path / "sweep.csv").read_bytes() == first_csv
    assert (tmp_path / "coverage.json").read_bytes() == first_coverage
    # Task ordering is normalized in output, but the source manifest hash
    # intentionally retains the identity of its original ordered JSON object.
    export(tmp_path, {**cohort, "tasks": list(reversed(cohort["tasks"]))}, sources)
    second_coverage = json.loads((tmp_path / "coverage.json").read_bytes())
    assert second_coverage["tasks"] == json.loads(first_coverage)["tasks"]
    assert (
        second_coverage["sources"]["manifest_content_sha256"]
        != coverage["sources"]["manifest_content_sha256"]
    )


def test_empty_observation_set_emits_header_not_fabricated_labels(tmp_path: Path) -> None:
    value = receipt(tmp_path)
    summary, rows, coverage = export(tmp_path, manifest(value), [])
    assert rows == []
    assert summary["classified_count"] == 0
    assert all(count == 0 for count in summary["counts_per_label"].values())
    assert coverage["tasks"][0]["state"] == "not-run"
    assert (tmp_path / "sweep.csv").read_text().startswith("task_id,label,fix_commit,")


@pytest.mark.parametrize("protected", ["receipt", "patch", "log", "coverage"])
def test_outputs_cannot_overwrite_evidence_or_each_other(tmp_path: Path, protected: str) -> None:
    value = receipt(tmp_path)
    source = observation(tmp_path, value)
    selected = {
        "receipt": source[0],
        "patch": Path(value["extraction"]["solution_patch_path"]),
        "log": Path(value["arms"]["nop"]["evidence_path"]),
        "coverage": tmp_path / "coverage.json",
    }[protected]
    original = selected.read_bytes() if selected.exists() else None
    with pytest.raises(ValueError):
        results.write_sweep(manifest(value), [source], selected, tmp_path / "coverage.json")
    if original is not None:
        assert selected.read_bytes() == original


def test_atomic_outputs_are_not_truncated_when_staging_fails(tmp_path: Path, monkeypatch) -> None:
    value = receipt(tmp_path)
    source = observation(tmp_path, value)
    csv_path, coverage_path = tmp_path / "sweep.csv", tmp_path / "coverage.json"
    csv_path.write_bytes(b"previous csv\n")
    coverage_path.write_bytes(b"previous coverage\n")
    original_fsync = results.os.fsync
    calls = 0

    def failing_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("coverage staging failed")
        original_fsync(fd)

    monkeypatch.setattr(results.os, "fsync", failing_fsync)
    with pytest.raises(OSError, match="coverage staging failed"):
        results.write_sweep(manifest(value), [source], csv_path, coverage_path)
    assert csv_path.read_bytes() == b"previous csv\n"
    assert coverage_path.read_bytes() == b"previous coverage\n"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_atomic_publish_rolls_back_csv_if_coverage_replace_fails(
    tmp_path: Path, monkeypatch
) -> None:
    value = receipt(tmp_path)
    source = observation(tmp_path, value)
    csv_path, coverage_path = tmp_path / "sweep.csv", tmp_path / "coverage.json"
    csv_path.write_bytes(b"previous csv\n")
    coverage_path.write_bytes(b"previous coverage\n")
    original_replace = results.os.replace

    def failing_replace(src, dst) -> None:
        assert Path(src).parent == Path(dst).parent
        if Path(dst) == coverage_path:
            raise OSError("coverage replacement failed")
        original_replace(src, dst)

    monkeypatch.setattr(results.os, "replace", failing_replace)
    with pytest.raises(OSError, match="coverage replacement failed"):
        results.write_sweep(manifest(value), [source], csv_path, coverage_path)
    assert csv_path.read_bytes() == b"previous csv\n"
    assert coverage_path.read_bytes() == b"previous coverage\n"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_cli_reads_real_sources_and_prints_actual_hashes(tmp_path: Path, capsys) -> None:
    value = receipt(tmp_path)
    source = observation(tmp_path, value)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest(value)), encoding="utf-8")
    csv_path, coverage_path = tmp_path / "sweep.csv", tmp_path / "coverage.json"
    assert (
        results.main(
            [
                "--manifest",
                str(manifest_path),
                "--receipts",
                str(source[0].parent),
                "--output",
                str(csv_path),
                "--coverage",
                str(coverage_path),
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["classified_count"] == 1
    assert report["sources"]["manifest"]["sha256"] == digest(manifest_path.read_bytes())
    assert report["sources"]["receipts"][0]["sha256"] == digest(source[0].read_bytes())
    assert report["csv_sha256"] == digest(csv_path.read_bytes())
    assert report["coverage_sha256"] == digest(coverage_path.read_bytes())


@pytest.mark.parametrize(
    "malformed",
    [
        '{"schema_version": 1, "schema_version": 2}',
        '{"schema_version": 1, "tasks": NaN}',
        "{",
        "[]",
    ],
)
def test_cli_rejects_malformed_json_before_writing(tmp_path: Path, malformed: str) -> None:
    receipt_dir = tmp_path / "receipts"
    receipt_dir.mkdir()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(malformed, encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        results.main(
            [
                "--manifest",
                str(manifest_path),
                "--receipts",
                str(receipt_dir),
                "--output",
                str(tmp_path / "sweep.csv"),
                "--coverage",
                str(tmp_path / "coverage.json"),
            ]
        )
    assert error.value.code == 2
    assert not (tmp_path / "sweep.csv").exists()
    assert not (tmp_path / "coverage.json").exists()


def test_scientific_csv_retains_all_six_labels_without_unknown_rows(tmp_path: Path) -> None:
    values = [receipt(tmp_path, number) for number in range(1, 8)]
    values[1]["arms"]["oracle"]["verifier_exit_code"] = 1
    add_open(values[1])
    values[2]["arms"]["oracle"]["verifier_exit_code"] = 1
    values[3]["extraction"]["status"] = "empty-diff"
    values[4]["extraction"]["status"] = "patch-no-apply"
    values[5]["arms"]["nop"]["verifier_exit_code"] = 0
    values[6]["arms"]["oracle"] = {"status": "infrastructure-error"}
    sources = [observation(tmp_path, value) for value in values]
    summary, rows, coverage = export(tmp_path, manifest(*values), sources)
    assert [row["label"] for row in rows] == [
        "oracle:pass+nop:fail",
        "oracle:fail-network",
        "oracle:fail",
        "oracle:none",
        "oracle:patch-conflict",
        "nop:pass",
    ]
    assert summary["classified_count"] == 6
    assert all(count == 1 for count in summary["counts_per_label"].values())
    assert coverage["tasks"][-1]["label"] is None
    assert coverage["tasks"][-1]["state"] == "infrastructure-error"


def test_source_json_boolean_cannot_be_silently_overridden_with_integer(tmp_path: Path) -> None:
    value = receipt(tmp_path)
    value["arms"]["nop"]["verifier_exit_code"] = False
    source = observation(tmp_path, value)
    value["arms"]["nop"]["verifier_exit_code"] = 0
    with pytest.raises(ValueError, match="differs from source JSON"):
        export(tmp_path, manifest(value), [source])


@pytest.mark.parametrize(
    "malformed",
    [
        {},
        {"schema_version": 1, "tasks": {}},
        {"schema_version": 1, "tasks": [None]},
        {"schema_version": True, "tasks": []},
    ],
)
def test_malformed_manifests_are_rejected_without_outputs(tmp_path: Path, malformed: dict) -> None:
    with pytest.raises(ValueError):
        export(tmp_path, malformed, [])
    assert not (tmp_path / "sweep.csv").exists()
    assert not (tmp_path / "coverage.json").exists()


@pytest.mark.parametrize(
    "field,bad",
    [
        ("fix", {"sha": "HEAD"}),
        ("tip", "branch/name"),
        ("strategy", ""),
        ("solution_patch_bytes", True),
        ("status", "unknown"),
        ("apply_check_on_base", False),
    ],
)
def test_malformed_success_extraction_cannot_produce_scientific_pass(
    tmp_path: Path,
    field: str,
    bad,
) -> None:
    value = receipt(tmp_path)
    value["extraction"][field] = bad
    assert results.classify_receipt(value)["label"] is None


def test_divergent_success_requires_explicit_patch_tip(tmp_path: Path) -> None:
    value = receipt(tmp_path)
    value["extraction"]["status"] = "ok-divergent"
    assert results.classify_receipt(value)["label"] is None


@pytest.mark.parametrize("status", ["budget-stopped", "timeout", "infrastructure-error", "not-run"])
def test_incomplete_open_confirmation_keeps_locked_failure_in_csv(
    tmp_path: Path,
    status: str,
) -> None:
    value = receipt(tmp_path)
    value["arms"]["oracle"]["verifier_exit_code"] = 1
    value["arms"]["open_oracle"] = {"status": status}
    summary, rows, coverage = export(tmp_path, manifest(value), [observation(tmp_path, value)])
    assert rows[0]["label"] == "oracle:fail"
    assert summary["classified_count"] == 1
    assert summary["unclassified_count"] == 0
    task = coverage["tasks"][0]
    assert task["state"] == "classified"
    assert task["network_confirmation"]["state"] == status
    assert task["network_confirmation"]["reason"]
    assert task["arm_statuses"]["open_oracle"] == status


@pytest.mark.parametrize("bad", [None, [], {}, {"status": "complete"}])
def test_malformed_open_confirmation_cannot_erase_or_promote_locked_failure(
    tmp_path: Path,
    bad,
) -> None:
    value = receipt(tmp_path)
    value["arms"]["oracle"]["verifier_exit_code"] = 1
    value["arms"]["open_oracle"] = bad
    verdict = results.classify_receipt(value)
    assert verdict["label"] == "oracle:fail"
    assert verdict["network_confirmation"]["state"] in ("invalid", "not-run", "source-mismatch")


def test_optional_confirmation_detail_tracks_absent_failed_and_confirmed(tmp_path: Path) -> None:
    value = receipt(tmp_path)
    value["arms"]["oracle"]["verifier_exit_code"] = 1
    verdict = results.classify_receipt(value)
    assert verdict["label"] == "oracle:fail"
    assert verdict["network_confirmation"]["state"] == "not-run"
    add_open(value, 1)
    verdict = results.classify_receipt(value)
    assert verdict["label"] == "oracle:fail"
    assert verdict["network_confirmation"]["state"] == "failed"
    value["arms"]["open_oracle"]["verifier_exit_code"] = 0
    verdict = results.classify_receipt(value)
    assert verdict["label"] == "oracle:fail-network"
    assert verdict["network_confirmation"]["state"] == "confirmed"


@pytest.mark.parametrize(
    "code,state",
    [
        (124, "timeout"),
        (137, "infrastructure-error"),
        (None, "infrastructure-error"),
        (True, "infrastructure-error"),
    ],
)
def test_abnormal_open_exit_cannot_erase_valid_locked_failure(
    tmp_path: Path,
    code,
    state: str,
) -> None:
    value = receipt(tmp_path)
    value["arms"]["oracle"]["verifier_exit_code"] = 1
    add_open(value)
    value["arms"]["open_oracle"]["verifier_exit_code"] = code
    assert_arm_problem(value, "open_oracle", state)
