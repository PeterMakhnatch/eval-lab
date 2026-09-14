"""HAR-25 behavioral contract for the harness-first cohort quality audit.

The audit must keep four questions separate (static screening, semantic
validity, difficulty, training utility), must never turn a declared training
allowance into an audited one, must never certify semantics from oracle/nop
controls, and must never map harness-``unsupported`` findings to ``invalid``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from evallab import cli, quality_audit
from evallab.quality_audit import audit_cohort
from evallab.registry import compute_task_digests
from evallab.schemas import TaskDigests
from evallab.task_workbench import CandidateSource, Diagnostic, Inspection

ROOT = Path(__file__).resolve().parents[1]
VALID = ROOT / "tests/fixtures/task_workbench/valid"

TASK_PATH = "library/synthetic/m007/uppercase-fixture"


def _repo_with_fixture(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    task = repo / TASK_PATH
    task.parent.mkdir(parents=True)
    shutil.copytree(VALID, task)
    return repo


def _member(**overrides: object) -> dict[str, object]:
    member: dict[str, object] = {
        "task_id": "fixture-task",
        "task_path": TASK_PATH,
        "provenance_zone": "02-local-evidence",
        "source_uri": "local/uppercase-fixture",
        "source_ref": "local/uppercase-fixture@1.0.0",
        "license": "MIT",
        "allowed_uses": ["measurement"],
        "control_evidence": {
            "oracle": {"reward": 1.0},
            "nop": {"reward": 0.0},
        },
    }
    member.update(overrides)
    return member


def _write_cohort(repo: Path, members: list[object]) -> Path:
    cohort = {
        "schema_version": 1,
        "cohort_id": "audit-test-cohort",
        "members": members,
    }
    path = repo / "cohort.json"
    path.write_text(json.dumps(cohort, indent=2), encoding="utf-8")
    return path


def _audit(repo: Path, members: list[object]) -> dict:
    return audit_cohort(repo, _write_cohort(repo, members))


def _single_member(report: dict) -> dict:
    assert len(report["members"]) == 1
    return report["members"][0]


def test_record_shape_and_cohort_digest(tmp_path: Path) -> None:
    repo = _repo_with_fixture(tmp_path)
    cohort_path = _write_cohort(repo, [_member()])
    raw = cohort_path.read_bytes()

    report = audit_cohort(repo, cohort_path)

    assert report["kind"] == "harness_first_quality_audit"
    assert report["cohort_id"] == "audit-test-cohort"
    assert report["cohort_digest"] == "sha256:" + hashlib.sha256(raw).hexdigest()
    member = _single_member(report)
    assert member["task_id"] == "fixture-task"
    # Editing the freeze changes its digest independently of the task bytes.
    edited = json.loads(raw)
    edited["members"][0]["task_id"] = "renamed"
    cohort_path.write_text(json.dumps(edited), encoding="utf-8")
    second = audit_cohort(repo, cohort_path)
    assert second["cohort_digest"] != report["cohort_digest"]


def test_training_utility_stays_unknown_despite_declared_training_use(
    tmp_path: Path,
) -> None:
    repo = _repo_with_fixture(tmp_path)
    report = _audit(repo, [_member(allowed_uses=["measurement", "training"])])
    member = _single_member(report)

    assert member["training_utility"]["status"] == "unknown"
    # The declaration is carried verbatim under an explicitly "declared" name;
    # the audit never emits its own allowed_uses verdict.
    assert member["declared_allowed_uses"] == ["measurement", "training"]
    assert "allowed_uses" not in member
    assert "eligible" not in json.dumps(member["training_utility"])


def test_semantic_validity_is_oracle_nop_only_and_never_certified(tmp_path: Path) -> None:
    repo = _repo_with_fixture(tmp_path)
    report = _audit(repo, [_member()])
    semantic = _single_member(report)["semantic_validity"]

    assert semantic["status"] == "oracle_nop_only"
    assert semantic.get("certified") is not True
    assert semantic["status"] != "certified"


@pytest.mark.parametrize(
    "control_evidence",
    [
        None,
        {},
        {"oracle": {"reward": 0.5}, "nop": {"reward": 0.0}},
        {"oracle": {"reward": 1.0}, "nop": {"reward": 1.0}},
        {"oracle": {"reward": 1.0}},
        {"oracle": {"reward": True}, "nop": {"reward": 0.0}},
    ],
)
def test_semantic_validity_is_unknown_without_oracle1_nop0(
    tmp_path: Path, control_evidence: object
) -> None:
    repo = _repo_with_fixture(tmp_path)
    report = _audit(repo, [_member(control_evidence=control_evidence)])
    semantic = _single_member(report)["semantic_validity"]

    assert semantic["status"] == "unknown"
    assert semantic.get("certified") is not True


def test_difficulty_always_unknown(tmp_path: Path) -> None:
    repo = _repo_with_fixture(tmp_path)
    report = _audit(repo, [_member()])
    assert _single_member(report)["difficulty"]["status"] == "unknown"


def test_static_screening_pass_on_clean_fixture(tmp_path: Path) -> None:
    repo = _repo_with_fixture(tmp_path)
    report = _audit(repo, [_member()])
    static = _single_member(report)["static_screening"]
    assert static["status"] == "pass"
    assert static["codes"] == []

@pytest.mark.parametrize("mutation", ["data", "verifier", "data_and_verifier"])
def test_frozen_pins_detect_statically_valid_task_drift(
    tmp_path: Path, mutation: str
) -> None:
    repo = _repo_with_fixture(tmp_path)
    task = repo / TASK_PATH
    pins = compute_task_digests(task).model_dump(mode="json")
    cohort_path = _write_cohort(repo, [_member(digests=pins)])
    before = audit_cohort(repo, cohort_path)
    frozen = _single_member(before)
    identity = frozen["static_screening"]["cohort_identity"]
    assert identity["status"] == "match"
    assert identity["actual_digests"] == pins
    assert identity["mismatches"] == []
    assert identity["inspected"]["task_id"] == "uppercase-fixture"
    assert identity["inspected"]["task_version"] == "1.0.0"

    changed_components = {"package"}
    if mutation in {"data", "data_and_verifier"}:
        (task / "environment/input.txt").write_text("a different input\n", encoding="utf-8")
        changed_components.add("environment")
    if mutation in {"verifier", "data_and_verifier"}:
        (task / "tests/golden.txt").write_text("A DIFFERENT INPUT\n", encoding="utf-8")
        changed_components.add("verifier")

    after = audit_cohort(repo, cohort_path)
    changed = _single_member(after)
    assert after["cohort_digest"] == before["cohort_digest"]
    assert changed["task_id"] == frozen["task_id"]
    assert changed["static_screening"]["status"] == "pass"
    drift = changed["static_screening"]["cohort_identity"]
    assert drift["status"] == "drift"
    assert drift["actual_digests"] == compute_task_digests(task).model_dump(mode="json")
    assert {item["component"] for item in drift["mismatches"]} == changed_components
    for item in drift["mismatches"]:
        assert item["expected"] == pins[item["component"]]
        assert item["actual"] != item["expected"]
    assert drift["inspected"]["candidate_id"] != identity["inspected"]["candidate_id"]
    assert drift["inspected"]["digests"]["package"] != identity["inspected"]["digests"]["package"]
    semantic = changed["semantic_validity"]
    assert semantic["status"] == "oracle_nop_only"
    assert semantic["certified"] is False
    assert semantic["scope"] == "cohort_control_declaration"
    assert semantic["authenticated_for_inspected_identity"] is False


def test_component_pin_mismatch_is_not_hidden_by_matching_package(tmp_path: Path) -> None:
    repo = _repo_with_fixture(tmp_path)
    pins = compute_task_digests(repo / TASK_PATH).model_dump(mode="json")
    pins["verifier"] = "sha256:" + "0" * 64
    identity = _single_member(_audit(repo, [_member(digests=pins)]))[
        "static_screening"
    ]["cohort_identity"]
    assert identity["status"] == "drift"
    assert identity["actual_digests"]["package"] == pins["package"]
    assert [item["component"] for item in identity["mismatches"]] == ["verifier"]


def test_text_audit_exposes_live_identity_and_frozen_drift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo_with_fixture(tmp_path)
    task = repo / TASK_PATH
    pins = compute_task_digests(task).model_dump(mode="json")
    cohort_path = _write_cohort(repo, [_member(digests=pins)])
    (task / "environment/input.txt").write_text("different data\n", encoding="utf-8")
    actual = compute_task_digests(task)

    assert cli.run_cli(
        ["quality", "audit", "--cohort", str(cohort_path)], workspace=repo
    ) == 0

    output = capsys.readouterr().out
    assert "cohort_identity: drift" in output
    assert "uppercase-fixture@1.0.0" in output
    assert actual.package in output
    assert pins["package"] in output
    assert actual.environment in output
    assert pins["environment"] in output


def test_registry_identity_changed_after_inspection_cannot_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo_with_fixture(tmp_path)
    task = repo / TASK_PATH
    pins = compute_task_digests(task).model_dump(mode="json")

    def changed_digests(task_dir: Path) -> TaskDigests:
        (task_dir / "environment/input.txt").write_text("changed during audit\n", encoding="utf-8")
        return compute_task_digests(task_dir)

    monkeypatch.setattr(quality_audit, "compute_task_digests", changed_digests)
    identity = _single_member(_audit(repo, [_member(digests=pins)]))[
        "static_screening"
    ]["cohort_identity"]
    assert identity["status"] == "unknown"
    assert identity["actual_digests"]["package"] != pins["package"]
    assert identity["inspected"]["digests"]["registry_package"] == pins["package"]


@pytest.mark.parametrize("pins", [None, {}, {"package": "not-a-digest"}])
def test_absent_or_malformed_pins_never_claim_an_identity_match(
    tmp_path: Path, pins: object
) -> None:
    repo = _repo_with_fixture(tmp_path)
    member = _single_member(_audit(repo, [_member(digests=pins)]))
    assert member["static_screening"]["status"] == "pass"
    identity = member["static_screening"]["cohort_identity"]
    assert identity["status"] == "unknown"
    assert identity["actual_digests"] == compute_task_digests(repo / TASK_PATH).model_dump(mode="json")



def test_pure_unsupported_diagnostics_are_not_invalid(tmp_path: Path) -> None:
    repo = _repo_with_fixture(tmp_path)
    # A package-local symlink trips a real harness limitation (Harbor build
    # context bytes must be regular files) without any safety violation.
    (repo / TASK_PATH / "inside-link.txt").symlink_to("environment/input.txt")

    report = _audit(repo, [_member()])
    static = _single_member(report)["static_screening"]

    assert static["status"] == "unsupported"
    assert static["status"] != "invalid"
    assert "symlink_unsupported" in static["codes"]
    assert static["cohort_identity"]["status"] == "unknown"
    assert static["cohort_identity"]["actual_digests"] is None


def test_safety_invalid_unauthorized_secret_codes_are_invalid(tmp_path: Path) -> None:
    repo = _repo_with_fixture(tmp_path)

    # task_toml_invalid: unparsable configuration.
    broken = repo / TASK_PATH
    (broken / "task.toml").write_text("not [ valid toml", encoding="utf-8")
    report = _audit(repo, [_member()])
    static = _single_member(report)["static_screening"]
    assert static["status"] == "invalid"
    assert "task_toml_invalid" in static["codes"]

    # path_escape: a symlink resolving outside the candidate package.
    repo2 = _repo_with_fixture(tmp_path / "escape")
    (repo2 / "outside.txt").write_text("outside\n", encoding="utf-8")
    (repo2 / TASK_PATH / "escape-link.txt").symlink_to("../../../outside.txt")
    static = _single_member(_audit(repo2, [_member()]))["static_screening"]
    assert static["status"] == "invalid"
    assert "path_escape" in static["codes"]

    # verifier_env_literal_secret: literal secret in verifier env.
    repo3 = _repo_with_fixture(tmp_path / "secret")
    task3 = repo3 / TASK_PATH
    toml = (task3 / "task.toml").read_text(encoding="utf-8")
    (task3 / "task.toml").write_text(
        toml + '\n[verifier.env]\nOPENAI_API_KEY = "sk-1234567890abcdef"\n',
        encoding="utf-8",
    )
    static = _single_member(_audit(repo3, [_member()]))["static_screening"]
    assert static["status"] == "invalid"
    assert "verifier_env_literal_secret" in static["codes"]


def test_marker_class_wins_over_unsupported_within_one_code_or_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo_with_fixture(tmp_path)

    def _inspection(*, codes: list[str]) -> Inspection:
        return Inspection(
            candidate={},
            diagnostics=tuple(
                Diagnostic(
                    severity="error",
                    code=code,
                    classification="task_defect",
                    path="$source",
                    message="injected diagnostic",
                )
                for code in codes
            ),
            control_plan=(),
        )

    real_inspect = quality_audit.inspect_candidate

    def _inspect(*, repo_root: Path, task_path: Path, source: CandidateSource) -> Inspection:
        del repo_root, task_path, source
        return _inspection(codes=_inspect.codes)  # type: ignore[attr-defined]

    # A single code that names both a secret violation and an unsupported
    # surface is invalid: the safety class wins over the limitation.
    _inspect.codes = ["verifier_secret_channel_unsupported"]  # type: ignore[attr-defined]
    monkeypatch.setattr(quality_audit, "inspect_candidate", _inspect)
    static = _single_member(_audit(repo, [_member()]))["static_screening"]
    assert static["status"] == "invalid"

    # A member mixing one pure-unsupported error with one marker-class error
    # reports the more severe invalid finding.
    _inspect.codes = ["mcp_transport_unsupported", "verifier_env_literal_secret"]
    static = _single_member(_audit(repo, [_member()]))["static_screening"]
    assert static["status"] == "invalid"

    # Only pure unsupported errors keep the member unsupported.
    _inspect.codes = ["mcp_transport_unsupported", "compose_privileged_unsupported"]
    static = _single_member(_audit(repo, [_member()]))["static_screening"]
    assert static["status"] == "unsupported"

    monkeypatch.setattr(quality_audit, "inspect_candidate", real_inspect)


def test_inspect_failure_degrades_to_unknown(tmp_path: Path) -> None:
    repo = _repo_with_fixture(tmp_path)
    report = _audit(repo, [_member(task_path="library/tasks/does-not-exist")])
    static = _single_member(report)["static_screening"]
    assert static["status"] == "unknown"
    assert static["codes"] == []
    identity = static["cohort_identity"]
    assert identity["status"] == "unknown"
    assert identity["inspected"] is None
    assert identity["actual_digests"] is None


def test_unpinned_or_missing_source_metadata_is_invalid_not_unsupported(
    tmp_path: Path,
) -> None:
    repo = _repo_with_fixture(tmp_path)
    report = _audit(repo, [_member(source_ref=None)])
    static = _single_member(report)["static_screening"]
    assert static["status"] == "invalid"
    assert "source_ref_unpinned" in static["codes"]


def test_malformed_members_degrade_to_unknown(tmp_path: Path) -> None:
    repo = _repo_with_fixture(tmp_path)
    report = _audit(repo, [_member(task_path=None), "not-an-object"])

    missing_path, non_object = report["members"]
    assert missing_path["static_screening"]["status"] == "unknown"
    # The four fields are independent: valid control evidence still classifies
    # semantic validity even when static screening could not run.
    assert missing_path["semantic_validity"]["status"] == "oracle_nop_only"
    assert missing_path["difficulty"]["status"] == "unknown"
    assert missing_path["training_utility"]["status"] == "unknown"
    assert missing_path["declared_allowed_uses"] == ["measurement"]
    for field in ("static_screening", "semantic_validity"):
        assert non_object[field]["status"] == "unknown"
    assert non_object["task_path"] is None


def test_cohort_file_must_be_a_member_carrying_object(tmp_path: Path) -> None:
    repo = _repo_with_fixture(tmp_path)
    bad = repo / "bad.json"
    bad.write_text('{"cohort_id": "x", "members": "nope"}', encoding="utf-8")
    with pytest.raises(ValueError):
        audit_cohort(repo, bad)
    array = repo / "array.json"
    array.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        audit_cohort(repo, array)
