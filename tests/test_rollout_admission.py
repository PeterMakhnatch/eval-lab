"""Structural rollout admission: the pure helpers and the single-file allow-list."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab import rollout_admission as ra
from evallab.line.io import digest as line_digest


class Registry:
    def assigned_test_anywhere(
        self, *, item_id: str | None = None, task_id: str | None = None
    ) -> bool:
        return "build-pmars" in (item_id, task_id)


@pytest.mark.parametrize(
    "value",
    [
        {"split": "test"},
        {"Split": "test"},
        {"SPLIT": "held-out"},
        {"split": "   "},
        {"split": ["test"]},
        {"split": 1},
        {"a": {"b": {"split": "test"}}},
        {"tasks": [{"split": "test"}]},
        {"metadata": {"split": "Test"}},
        {"steps": [{"tool_calls": [{"arguments": {"split": "test"}}]}]},
    ],
)
def test_any_split_that_is_not_exactly_dev_is_refused(value: dict) -> None:
    assert ra.declares_non_dev(value)


@pytest.mark.parametrize("value", [{}, {"split": "dev"}, {"split": " DEV "}, {"split": None}])
def test_dev_or_absent_splits_are_accepted(value: dict) -> None:
    assert not ra.declares_non_dev(value)


def test_a_split_inside_agent_written_steps_is_refused_too() -> None:
    assert ra.declares_non_dev({"steps": [{"split": "test"}]})


def test_a_document_nested_past_the_limit_is_refused_even_without_a_split() -> None:
    nested: dict = {"note": 1}
    for _ in range(ra.DOCUMENT_DEPTH + 2):
        nested = {"inner": nested}
    assert ra.declares_non_dev(nested)


def test_name_forms_cover_segments_stems_case_and_trailing_separators() -> None:
    forms = ra.name_forms("/Runs/TB2/Build-PMARS__123/")
    assert {"build-pmars__123", "build-pmars", "tb2", "runs"} <= forms
    assert "build-pmars" in ra.name_forms("build-pmars.json")
    assert "build-pmars" in ra.name_forms("Build-Pmars__123.TXT")
    assert "build-pmars" in ra.name_forms("build-pmars.tar.gz")
    assert "build-pmars" in ra.name_forms("tasks\\Build-Pmars")
    assert "build-pmars" in ra.name_forms("\uff42uild-pmars")  # NFKC folds the fullwidth b


def _single(tmp: Path, folder: str = "singles") -> Path:
    file = tmp / folder / "trajectory.json"
    file.parent.mkdir()
    file.write_text(json.dumps({"steps": []}), encoding="utf-8")
    return file


def test_a_plain_single_file_is_admitted(tmp_path: Path) -> None:
    file = _single(tmp_path)
    admitted = ra.admit_rollout(str(file), registry=Registry(), run_id="r1")
    assert admitted.kind == "file" and admitted.trajectory == file


def test_the_listed_path_must_be_canonical(tmp_path: Path) -> None:
    file = _single(tmp_path)
    for text in (str(file.parent) + "/../singles/trajectory.json", "relative/path.json"):
        with pytest.raises(ra.AdmissionRefused, match="not canonical"):
            ra.admit_rollout(text, registry=Registry())


def test_a_registry_test_name_in_the_run_id_is_refused(tmp_path: Path) -> None:
    file = _single(tmp_path)
    with pytest.raises(ra.AdmissionRefused, match="assigned TEST"):
        ra.admit_rollout(str(file), registry=Registry(), run_id="build-pmars__9")


def _trial(tmp: Path, name: str = "trial__X", *, agent: bool = True) -> Path:
    trial = tmp / name
    (trial / "verifier").mkdir(parents=True)
    if agent:
        (trial / "agent").mkdir()
        (trial / "agent" / "trajectory.json").write_text(
            json.dumps({"steps": [{"step_id": 1, "message": "original"}]}), encoding="utf-8"
        )
    (trial / "result.json").write_text(json.dumps({"task_name": "fine"}), encoding="utf-8")
    (trial / "verifier" / "agent.diff").write_text("diff --git a/x b/x\n", encoding="utf-8")
    return trial


def test_the_digest_is_the_lines_and_a_wrong_one_is_an_integrity_refusal(tmp_path: Path) -> None:
    trial = _trial(tmp_path)
    expected = line_digest(trial)
    admitted = ra.admit_rollout(str(trial), registry=Registry(), expected_sha256=expected)
    assert admitted.sha256 == expected and admitted.kind == "trial"
    single = _single(tmp_path, "solo")
    assert ra.admit_rollout(str(single), registry=Registry()).sha256 == line_digest(single)
    with pytest.raises(ra.AdmissionRefused, match="sha256 expected") as refused:
        ra.admit_rollout(str(trial), registry=Registry(), expected_sha256="ab" * 32)
    assert refused.value.integrity and not refused.value.unreadable


def test_the_document_served_is_the_one_the_digest_covered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trial = _trial(tmp_path)
    expected = line_digest(trial)
    read_tree = ra._read_tree

    def swap_after_reading(
        root: Path, sealed: frozenset, *, retain_copy_outputs: bool = False
    ) -> object:
        tree = read_tree(root, sealed, retain_copy_outputs=retain_copy_outputs)
        (root / "agent" / "trajectory.json").write_text('{"steps": []}', encoding="utf-8")
        (root / "verifier" / "agent.diff").write_text("SWAPPED", encoding="utf-8")
        return tree

    monkeypatch.setattr(ra, "_read_tree", swap_after_reading)
    admitted = ra.admit_rollout(str(trial), registry=Registry(), expected_sha256=expected)
    assert admitted.document["steps"][0]["message"] == "original"
    assert admitted.diff == "diff --git a/x b/x\n"


def test_a_bundle_without_a_trajectory_is_admitted_only_when_asked(tmp_path: Path) -> None:
    trial = _trial(tmp_path, agent=False)
    with pytest.raises(ra.AdmissionRefused, match="no ATIF trajectory"):
        ra.admit_rollout(str(trial), registry=Registry())
    admitted = ra.admit_rollout(str(trial), registry=Registry(), require_trajectory=False)
    assert admitted.document is None and admitted.trajectory is None
    assert admitted.diff == "diff --git a/x b/x\n"


def test_a_json_too_deep_to_parse_is_an_unreadable_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def too_deep(_text: str, **_options: object) -> object:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(ra.json, "loads", too_deep)
    with pytest.raises(ra.AdmissionRefused, match="not JSON") as refused:
        ra.admit_rollout(str(_single(tmp_path)), registry=Registry())
    assert refused.value.unreadable


def test_a_name_that_differs_only_by_case_from_the_disk_is_refused(tmp_path: Path) -> None:
    (tmp_path / "Probe").write_text("x", encoding="utf-8")
    if (tmp_path / "probe").exists():
        file = _single(tmp_path, "Folder")
        listed = str(file).replace("Folder", "folder")
        with pytest.raises(ra.AdmissionRefused, match="case does not match"):
            ra.admit_rollout(listed, registry=Registry())
    else:  # a case-sensitive filesystem cannot reach a file by the wrong case at all
        with pytest.raises(ra.AdmissionRefused, match="cannot stat"):
            ra.admit_rollout(str(tmp_path / "PROBE"), registry=Registry())


def test_a_link_anywhere_on_the_path_is_refused_so_none_can_hide_a_name(tmp_path: Path) -> None:
    target = tmp_path / "build-pmars"  # a TEST task name, reached only through a link
    (target / "runs").mkdir(parents=True)
    trial = _trial(target / "runs")
    (tmp_path / "top").symlink_to(target, target_is_directory=True)
    (target / "runs" / "inner").symlink_to(trial, target_is_directory=True)
    for listed in (
        tmp_path / "top" / "runs" / trial.name,  # a link above
        tmp_path / "top" / "runs",  # the folder reached through a link is itself the rollout
        target / "runs" / "inner",  # a link at the rollout
    ):
        with pytest.raises(ra.AdmissionRefused, match="symlink on the rollout path"):
            ra.admit_rollout(str(listed), registry=Registry())
    # the canonical spelling is examined, and its names are what the registry sees
    with pytest.raises(ra.AdmissionRefused, match="assigned TEST"):
        ra.admit_rollout(str(trial), registry=Registry())


def test_a_link_above_into_a_trials_agent_folder_is_refused(tmp_path: Path) -> None:
    trial = _trial(tmp_path)
    (tmp_path / "alias").symlink_to(trial / "agent", target_is_directory=True)
    with pytest.raises(ra.AdmissionRefused, match="symlink on the rollout path"):
        ra.admit_rollout(str(tmp_path / "alias" / "trajectory.json"), registry=Registry())


def test_a_path_that_is_not_its_own_real_path_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """macOS /tmp vs /private/tmp: only the canonical spelling is accepted."""
    file = _single(tmp_path)
    real = ra.os.path.realpath
    monkeypatch.setattr(
        ra.os.path, "realpath", lambda p, **kw: "/private" + p if p == str(file) else real(p, **kw)
    )
    with pytest.raises(ra.AdmissionRefused, match="not its own real path"):
        ra.admit_rollout(str(file), registry=Registry())


@pytest.mark.parametrize("where", ["sub", "agent/sub", "verifier/sub"])
def test_a_folder_rollout_inside_a_trial_is_refused(tmp_path: Path, where: str) -> None:
    """B2.2: <trial>/sub holding a trajectory would skip <trial>/result.json (a TEST trial)."""
    trial = _trial(tmp_path, "build-pmars__123")
    (trial / "result.json").write_text(json.dumps({"split": "test"}), encoding="utf-8")
    folder = trial / where
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "trajectory.json").write_text(json.dumps({"steps": []}), encoding="utf-8")
    with pytest.raises(ra.AdmissionRefused, match="folder inside a trial"):
        ra.admit_rollout(str(folder), registry=Registry())


TRIAL_RESULT = {
    "id": "x",
    "task_name": "tb2/build-pmars",
    "trial_name": "build-pmars__123",
    "verifier_result": {"rewards": {"reward": 1.0}},
    "split": "test",
}
TRIAL_CONFIG = {"task": {"path": "/x/build-pmars"}, "trial_name": "build-pmars__123"}
JOB_RESULT = {"id": "j", "n_total_trials": 4, "stats": {"n_trials": 4}}
JOB_CONFIG = {"job_name": "sweep", "jobs_dir": "/x", "tasks": [{"path": "/x/t"}], "agents": []}


def _bare_trial(tmp: Path, *, verifier: bool = False, sidecars: dict | None = None) -> Path:
    """A TEST trial with sidecars but no agent/ folder and no root trajectory.json (F1-F4)."""
    trial = tmp / "build-pmars__123"
    trial.mkdir()
    for name, document in (sidecars if sidecars is not None else {}).items():
        (trial / name).write_text(json.dumps(document), encoding="utf-8")
    if verifier:
        (trial / "verifier").mkdir()
        (trial / "verifier" / "agent.diff").write_text("d\n", encoding="utf-8")
    return trial


@pytest.mark.parametrize(
    ("name", "verifier", "sidecars", "where"),
    [
        ("F1-result", False, {"result.json": TRIAL_RESULT}, "logs"),
        (
            "F1-all-sidecars",
            False,
            {
                "result.json": TRIAL_RESULT,
                "config.json": TRIAL_CONFIG,
                "run.json": {"task": "t"},
                "grade.json": {"case_id": "c"},
            },
            "logs",
        ),
        ("F2-verifier", True, {"result.json": TRIAL_RESULT}, "logs"),
        ("F2-verifier-only", True, {}, "logs"),
        ("F3-nested-agent", False, {"result.json": TRIAL_RESULT}, "logs/agent"),
        ("config-only", False, {"config.json": TRIAL_CONFIG}, "logs"),
        ("unreadable-sidecar", False, {}, "logs"),
    ],
)
def test_a_folder_in_a_trial_without_agent_or_trajectory_is_refused_by_shape(
    tmp_path: Path, name: str, verifier: bool, sidecars: dict, where: str
) -> None:
    trial = _bare_trial(tmp_path, verifier=verifier, sidecars=sidecars)
    if name == "unreadable-sidecar":
        (trial / "result.json").write_text("{not json", encoding="utf-8")
    folder = trial / where
    folder.mkdir(parents=True)
    (folder / "trajectory.json").write_text(json.dumps({"steps": []}), encoding="utf-8")
    with pytest.raises(ra.AdmissionRefused, match="folder inside a trial"):
        ra.admit_rollout(str(folder), registry=Registry())


def test_a_job_folder_with_job_shaped_sidecars_above_trials_still_admits_them(
    tmp_path: Path,
) -> None:
    job = tmp_path / "job"
    job.mkdir()
    (job / "result.json").write_text(json.dumps(JOB_RESULT), encoding="utf-8")
    (job / "config.json").write_text(json.dumps(JOB_CONFIG), encoding="utf-8")
    (job / "run.json").write_text(json.dumps({"run_id": "r", "invocations": []}), encoding="utf-8")
    (job / "job.log").write_text("log\n", encoding="utf-8")
    trial = _trial(job)
    assert ra.admit_rollout(str(trial), registry=Registry()).kind == "trial"


def test_a_job_folder_with_a_trial_shaped_result_is_not_a_job(tmp_path: Path) -> None:
    """The shape decides, not the file name: a result.json with a trial's keys makes a trial."""
    job = tmp_path / "job"
    job.mkdir()
    (job / "result.json").write_text(json.dumps({"trial_name": "t"}), encoding="utf-8")
    with pytest.raises(ra.AdmissionRefused, match="folder inside a trial"):
        ra.admit_rollout(str(_trial(job)), registry=Registry())


class SealedRegistry(Registry):
    def sealed_identity(self, *, item_id=None, task_id=None, task_digest=None):
        return "digest" if task_digest == "sealed-digest" else None


def _neutral_trial(tmp: Path, sidecars: dict[str, object]) -> Path:
    """A trial whose folder name spells nothing, as in real Harbor (``<job>__<id>``): the task
    lives only in its sidecars, and the row lists a folder inside it."""
    trial = tmp / "har120-000838-a1__YSe3G7t"
    (trial / "logs").mkdir(parents=True)
    (trial / "logs" / "trajectory.json").write_text(json.dumps({"steps": []}), encoding="utf-8")
    for name, document in sidecars.items():
        (trial / name).write_text(json.dumps(document), encoding="utf-8")
    return trial / "logs"


@pytest.mark.parametrize(
    "sidecars",
    [
        {"result.json": {"Task_Name": "tb2/build-pmars"}},  # mixed-case key
        {"result.json": {"TASK_ID": {"Path": "/x/build-pmars"}}},
        {"result.json": {"Split": "test"}},
        {"result.json": {"split": "test"}},  # only a split
        {"config.json": {"task_id": "build-pmars"}},  # only an identity
        {"result.json": {"task_checksum": "sealed-digest"}},
        {"result.json": {}},  # empty mapping: fail closed
        {"RESULT.JSON": {"split": "test"}},  # file name in another case
        {"provenance.json": {"task": "build-pmars"}},
    ],
)
def test_a_folder_in_a_neutrally_named_trial_is_held_to_the_trials_sidecars(
    tmp_path: Path, sidecars: dict[str, object]
) -> None:
    folder = _neutral_trial(tmp_path, sidecars)
    with pytest.raises(ra.AdmissionRefused):
        ra.admit_rollout(str(folder), registry=SealedRegistry())


@pytest.mark.parametrize(
    ("sidecars", "why"),
    [
        (
            {"config.json": {"job_name": "j", "tasks": [{"path": "/x/build-pmars"}]}},
            "assigned TEST",
        ),
        ({"config.json": {"jobs_dir": "/x", "TASKS": ["build-pmars"]}}, "assigned TEST"),
        ({"result.json": {"n_total_trials": 4, "stats": {}, "Split": "test"}}, "split"),
        ({"provenance.json": {"scope": {"split": "held-out"}}}, "split"),
        ({"CONFIG.JSON": {"tasks": [{"name": "build-pmars"}]}}, "assigned TEST"),
        (
            {"run.json": {"invocations": [], "task_digest": "sealed-digest"}},
            "sealed|inside a trial",
        ),
        ({"result.json": "[1, 2"}, "not JSON|inside a trial"),
    ],
)
def test_a_job_above_a_trial_is_held_to_the_same_checks_as_the_trial(
    tmp_path: Path, sidecars: dict[str, object], why: str
) -> None:
    """A job-shaped sidecar is not a free pass: a TEST identity or split above still refuses."""
    job = tmp_path / "job"
    job.mkdir()
    for name, document in sidecars.items():
        text = document if isinstance(document, str) else json.dumps(document)
        (job / name).write_text(text, encoding="utf-8")
    with pytest.raises(ra.AdmissionRefused, match=why):
        ra.admit_rollout(str(_trial(job)), registry=SealedRegistry())


def test_ancestor_sidecars_of_an_honest_job_pass(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    (job / "config.json").write_text(
        json.dumps({"job_name": "j", "tasks": [{"path": "/x/fine-task"}]}), encoding="utf-8"
    )
    (job / "result.json").write_text(json.dumps(JOB_RESULT), encoding="utf-8")
    (job / "provenance.json").write_text(json.dumps({"scope": {"split": "dev"}}), encoding="utf-8")
    assert ra.admit_rollout(str(_trial(job)), registry=SealedRegistry()).kind == "trial"


def test_mixed_case_keys_are_shape_keys_and_an_empty_sidecar_is_a_trial(tmp_path: Path) -> None:
    for index, document in enumerate(({"Trial_Name": "t"}, {}, {"TASK_CHECKSUM": "x"})):
        job = tmp_path / f"job{index}"
        job.mkdir()
        (job / "result.json").write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(ra.AdmissionRefused, match="folder inside a trial"):
            ra.admit_rollout(str(_trial(job)), registry=Registry())
